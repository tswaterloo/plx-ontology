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
from typing import Any, Callable, Mapping, Optional, Sequence

from operating import deep_investigation as deep
from operating.card_text import masked_spawn

from .errors import RecordNotFound

AGENT = "plx.agent.investigate"
ASK_SECONDS = deep.DEEP_SECONDS   # the hard cap of one Ask's session; each question's own
# budget is ``deep.question_budget`` under it (a lookup short, a multi-step question deep)
MAX_TURNS = deep.MAX_TURNS           # as the scoreboard's sessions
MAX_STEPS = deep.MAX_STEPS           # tool calls besides the answer gate's
# Helper hosts per Ask (``operating.host_peers``): its independent reads run at once.
# ``PLX_ASK_HOST_HELPERS`` sets another number (0: one host serves every call in turn).
HOST_HELPERS_ENV = "PLX_ASK_HOST_HELPERS"


def host_helpers() -> int:
    from operating.agent_session import HOST_HELPERS
    try:
        return max(0, min(4, int(os.environ.get(HOST_HELPERS_ENV, HOST_HELPERS))))
    except ValueError:
        return HOST_HELPERS
CATALOG_SECONDS = 3600     # a reader's tool catalog is listed again after this
# A reader's spare host is started again at most this often by a warm-up (``prewarm``);
# a spare is kept agent_session.SPARE_SECONDS, so a warm-up just before it expires renews it.
PREWARM_SECONDS = 240.0
_PREWARMED: dict[str, float] = {}
_PREWARMED_LOCK = threading.Lock()


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


# The Claude Code CLI's own words when the login's quota is spent ("You've hit your weekly
# limit", "Claude AI usage limit reached") or the login is not usable; matched only on a
# session that produced no answer and ended in an error (``session_failure``).
LIMIT_TEXT = re.compile(r"hit your (?:\w+[ -])?limit|usage limit|limit reached|rate[ -]limit"
                        r"|out of (?:extra )?usage|quota (?:exceeded|reached)|overloaded",
                        re.I)
AUTH_TEXT = re.compile(r"invalid api key|run /login|please log ?in|not logged in"
                       r"|oauth token (?:has )?expired|authentication[_ ](?:error|failed)"
                       r"|unauthori[sz]ed|credit balance is too low", re.I)
# the CLI's own words when the conversation no longer fits the model's context window
# (a session that ended in an error with no answer: ``session_failure``): the CLI's one message
# for it, read from its result text. Live-unverified: no run of this server has hit it yet.
CONTEXT_FULL_MESSAGE = "prompt is too long"
FAILURES = ("usage_limit", "sign_in", "timeout", "context_full", "error")


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
    if CONTEXT_FULL_MESSAGE in text.lower():
        return "context_full"
    return "error" if errored else None


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
                 max_turns: int = MAX_TURNS,
                 host_for: Callable[[Any], Any] | None = None,
                 agent_for: Callable[[Callable[[str], None]], Any] | None = None) -> None:
        self.state_dir, self.host_config, self.secret = state_dir, host_config, secret
        self.seconds, self.claude_bin, self.model = seconds, claude_bin, model
        self.max_turns = max_turns
        self._host_for, self._agent_for = host_for, agent_for

    @classmethod
    def from_args(cls, state_dir: Path, host_config: Path | None,
                  environ: Mapping[str, str], **options: Any) -> Optional["AgentAsk"]:
        """The API's agent path, or None when it cannot run here (no host config or no
        session secret): the Ask then stays on the console's objective box."""
        from operating.session_identity import session_secret

        if cls.missing(host_config, environ, options.get("claude_bin") or "claude"):
            return None
        secret = session_secret(environ)
        return cls(state_dir=Path(state_dir), host_config=Path(host_config), secret=secret,
                   **options)

    @staticmethod
    def missing(host_config: Path | None, environ: Mapping[str, str],
                claude_bin: str = "claude") -> Optional[str]:
        """Why the agent path cannot run here (``from_args``), as a code: ``no_host_config``,
        ``no_session_secret`` or ``no_claude_cli`` (the CLI is not on this process's PATH:
        a systemd unit's PATH lacks ``~/.local/bin``); None when it can."""
        import shutil
        from operating.session_identity import session_secret

        if host_config is None or not Path(host_config).is_file():
            return "no_host_config"
        if not session_secret(environ):
            return "no_session_secret"
        if shutil.which(claude_bin, path=environ.get("PATH")) is None:
            return "no_claude_cli"
        return None

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

    def prewarm(self, session: Any) -> bool:
        """Take the reader's first Ask's start-up off its path, in the background: list
        their tool catalog (``_Catalogued``, kept an hour) and start their spare host
        (``agent_session.HostSpares``: past its imports, grant and store generation, its
        search models and file indexes loading), so the session that follows takes it.
        At most once per reader per ``PREWARM_SECONDS``; False when nothing was started
        (a reader with no employee identity, or warmed recently). Never raises."""
        if self._host_for is not None or self._agent_for is not None:
            return False
        try:
            key = ticket_claims(session)["entraObjectId"]
        except ReaderRefused:
            return False
        now = time.monotonic()
        with _PREWARMED_LOCK:
            if now - _PREWARMED.get(key, -PREWARM_SECONDS) < PREWARM_SECONDS:
                return False
            _PREWARMED[key] = now

        def run() -> None:
            try:
                host = self.host_for(session)
                host.tools()
                agent = self.agent_for(lambda line: None)
                agent._start_spare(host)
                agent.warm_pool(host)   # hosts any reader's next session can bind
            except Exception:  # noqa: BLE001 - a warm-up never fails an Ask
                logging.getLogger("operator_api.ask").info(json.dumps(
                    {"prewarm": "failed"}))
        threading.Thread(target=run, name="ask-prewarm", daemon=True).start()
        return True

    def read_source(self, session: Any, handle: str, text_offset: int = 0
                    ) -> dict[str, Any]:
        """A captured original (``original://``), SharePoint file (``sharepoint://``) or
        byte-pinned mail attachment (``graph-mail://...#attachment=<id>&sha256=<digest>``:
        read only when the capture's per-reader gate shows the reader the parent message)
        as this reader's own host reads it: its
        ``plx.read`` through the capture store's per-reader gate
        (``operating.captured_originals``), the gate the agent's reads passed. Read-only;
        envelopes removed. ``KeyError`` when the host does not serve it to this reader
        (refused, unknown or unreadable alike), ``ReaderRefused`` for a reader with no
        employee identity."""
        if not isinstance(handle, str) or not (
                handle.startswith(("original://", "sharepoint://"))
                or _is_pinned_mail_attachment(handle)):
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
            raise RecordNotFound("citation_unresolvable_for_actor")
        return dict(got)


def reply_prose(final_text: str, answer: Optional[Mapping[str, Any]]) -> str:
    """The reader's answer in sentences: the gated message around the answer block (the
    agent's prose, every sentence the gate could not back already removed), else the
    answer's own text; never the json block."""
    from operating.answer_contract import _JSON_FENCE

    from operating.deep_investigation import STOPPED_LINE, STOPPED_WORDS

    prose = _JSON_FENCE.sub(" ", final_text or "")
    prose = "\n".join(line.rstrip() for line in prose.strip().splitlines())
    while "\n\n\n" in prose:
        prose = prose.replace("\n\n\n", "\n\n")
    text = str((answer or {}).get("text") or "").strip()
    if prose.strip() == STOPPED_LINE and text:
        # The research-stopped line is all the message says around the block (a reply
        # rendered from the gate's decision): it leads the answer's text, never stands
        # in for it (a not-found answer showed only "I stopped researching ...").
        # (the agent's own words when its text says it stopped: never twice)
        return text if STOPPED_WORDS in text.casefold() else STOPPED_LINE + "\n\n" + text
    return prose.strip() or text


def _is_pinned_mail_attachment(handle: str) -> bool:
    """A ``graph-mail://`` handle naming one attachment by its bytes (``#attachment=<id>
    &sha256=<64 hex>``): the only mail handle ``read_source`` takes (a message is read with
    ``plx.open``)."""
    import re
    return bool(re.fullmatch(r"graph-mail://[^#\s]+#attachment=[^&\s]+&sha256=[0-9a-f]{64}",
                             handle))
