"""The real backend of the app API: the console's object workspace, per reader, reshaped.

Nothing here answers on its own. Every read goes through the reader's own object workspace
(``operator_ui.case_console.object_workspace_for``, the one factory of the console's and
the Ask eval's workspaces: the case host's tools, grants and item gates, decided per
request), and Ask is the console's one question path (``operator_ui.objective_box``, the
employee agent loop whose claims pass ``operating.answer_gate``). This module only maps
what that path shows the reader to the contract's payload (``operator_api.models``):

* facts: the claims the loop kept and the workspace shows this reader (gate passed, every
  citation openable by them), each source with the value the gate checked (``fields``) or
  the quoted passage the gate matched (``quote``); plus the record page's own cited
  answers (code over cited records, as the console shows them). A vector never supplies a
  value: search only finds records the loop then reads.
* hidden: claims the loop kept but the reader may not open, counted by source family,
  never their text, title, sender or handle; a search result's hidden count (the ``SearchResult.hidden``) adds its by-owner counts when that search is wired.
* conflicts: the subject records' own conflicts (``plx.object``'s value disagreements;
  attested document conflicts), only values whose citation the reader may open, the
  trusted one the declared authority (lowest ``authority_rank``) with the reason, never a
  silent pick (no declared authority: none trusted, both shown).
* not found: the loop's unknowns and gaps, unfound identifiers, the plan answers' missing
  steps, with who would know (the Portal department that owns the step).
* freshness: the projection's build time and the host ledger's per-source capture times
  (read-only).

Threading: the case runtime's and the workspaces' stores are SQLite connections bound to
the thread that opened them, so every workspace call runs on that one owner thread
(``Worker``), as the console's one-thread server does; queued page reads go before queued
Asks, but a running Ask holds the thread (suggestions from Postgres need only the reader's
cached grant).

Evidence: isolated-fixture (the console's fixture world with a scripted planner in the
tests); live-unverified until the verifier runs the app over the host's state.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

from . import errors
from . import models as m
from .auth import Reader

MAX_WORKSPACES = 64
MAX_FACTS = 12
MAX_SOURCES_PER_FACT = 4
MAX_ANSWERS_KEPT = 2000
ASK_TIMEOUT_SECONDS = 600
ASK_WORKERS = 2
MAX_QUEUED_ASKS = 8
READ_TIMEOUT_SECONDS = 60
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) \
    / "plx-workbench-app"

# Portal Department codes (``operating.answerers``) in words, for "ask <team>".
DEPARTMENTS = {"ACCTMGMT": "Account Management", "ARTWORK": "Artwork", "COMPOUND": "Compounding",
               "EXEC": "Leadership", "FIN": "Finance", "HR": "HR", "IT": "IT",
               "MAINT": "Maintenance", "OPS": "Operations", "PROC": "Purchasing",
               "RND": "R&D", "PROD": "Production", "QUALITY": "Quality", "QA": "Quality "
               "Assurance", "QC": "Quality Control", "SALES": "Sales", "SHIP": "Shipping",
               "WH": "Warehouse"}

# L1 handle scheme -> the contract's system label.
SCHEMES = (("bc://", "BC"), ("api://", "Portal"), ("graph-mail://", "Email"),
           ("mail://", "Email"), ("teams", "Teams"), ("sharepoint", "File"),
           ("original://", "File"), ("repo://", "File"), ("mc://", "MC"),
           ("sage", "Sage"), ("filemaker", "FileMaker"), ("fm://", "FileMaker"))
# Whom to ask about an item of a family the reader may not open (its owner, in words).
FAMILY_OWNER = {"BC": "Finance (Business Central owners)", "Portal": "the Portal admins",
                "Email": "the mailbox owner", "Teams": "the channel or chat members",
                "File": "the document's owner", "MC": "the Mission Control bucket owner",
                "Sage": "Finance", "FileMaker": "Operations"}
# Host ledger sources -> system labels.
LEDGER_SYSTEMS = {"portal": "Portal", "bc": "BC", "mail": "Email", "sharepoint": "File",
                  "sharepoint_lists": "File", "teams": "Teams", "mc": "MC"}
STEP_WORDS = {"plx.find": "Looking up records", "plx.object": "Reading a record",
              "plx.around": "Following links between records",
              "plx.timeline": "Reading the timeline", "plx.documents": "Searching documents "
              "and email", "plx.read": "Reading a document", "plx.investigate":
              "Running a checked analysis", "plx.aggregate": "Adding up figures",
              "plx.process": "Checking the process steps", "plx.compare": "Comparing records",
              "plx.identity": "Checking which records are the same"}
INTENT_RULES = (("how_much", r"\b(cost|price|landed|margin|how much|total|amount|freight|"
                             r"duty)\b"),
                ("who", r"\b(who|whom|contact|owner|buyer|supplier for)\b"),
                ("why", r"\b(why|late|delay\w*|stuck|behind)\b"),
                ("timeline", r"\b(when|history|timeline|since)\b"),
                ("find_document", r"\b(document|pdf|file|email|quote|invoice|spec)\b"),
                ("compare", r"\b(compare|versus|vs\.?|difference)\b"),
                ("policy", r"\b(policy|rule|allowed|should we)\b"),
                ("status", r"\b(status|where is|update|open|received)\b"))


def system_of(handle: str) -> str:
    for prefix, system in SCHEMES:
        if handle.startswith(prefix):
            return system
    return "File"


def record_words(handle: str) -> str:
    """'Portal purchase order' from api://public.app_purchase_order/..., no value."""
    system = system_of(handle)
    path = urlsplit(handle)
    part = (path.netloc or "").split("/")[0] or (path.path.strip("/").split("/") or [""])[0]
    part = unquote(part).split(".")[-1]
    part = re.sub(r"^app_", "", part)
    part = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part).replace("_", " ").strip().lower()
    if system == "Email":
        return "Email message"
    if system == "Teams":
        return "Teams message"
    if system == "File":
        return "Document"
    return f"{system} {part}".strip() if part and len(part) <= 40 else system


def open_url(handle: str) -> str:
    return f"/source?handle={quote(handle, safe='')}"


def object_url(object_type: str, object_id: str) -> str:
    return f"/object/{quote(object_type or 'Object', safe='')}/{quote(object_id, safe='')}"


def plain(value: Any) -> Optional[str]:
    """A shown value as text (an ``untrusted_text`` envelope's text; None for none)."""
    if isinstance(value, Mapping):
        if "untrusted_text" in value:
            return str(value["untrusted_text"])[:500]
        if "value" in value and isinstance(value["value"], (str, int, float)):
            unit = value.get("unit") or value.get("currency")
            return f"{value['value']} {unit}".strip() if unit else str(value["value"])
        return None
    if value is None or isinstance(value, bool):
        return None if value is None else ("yes" if value else "no")
    return str(value)[:500]


def teams(codes: Iterable[Any]) -> str:
    words = []
    for code in codes or ():
        if not isinstance(code, str):
            continue
        if code.startswith("undeclared:"):
            continue
        word = DEPARTMENTS.get(code)
        if word and word not in words:
            words.append(word)
    return " or ".join(words)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---- the owner thread: every workspace call runs on it ----------------------------------

class Worker:
    """One owner thread: the thread that built a case runtime runs every workspace call
    over it (the runtime's and the workspaces' SQLite connections are bound to it, as in
    the console's one-thread server). Routes submit jobs from their own threads; ``loop``
    runs them on the owner thread in priority order. Several workers may share one job
    queue (the Ask pool: each its own runtime, built on its own thread). The readers'
    workspaces are cached per worker, built and used on its owner thread only."""

    def __init__(self, objects_for: Callable[[Any], Any],
                 jobs: "queue.PriorityQueue | None" = None) -> None:
        self.objects_for = objects_for
        self.jobs: "queue.PriorityQueue[tuple[int, int, Any]]" = \
            jobs if jobs is not None else queue.PriorityQueue()
        self.cache: dict[str, Any] = {}
        self.stopped = threading.Event()

    def _workspace(self, session: Any) -> Any:
        key = ",".join(sorted(session.principals))
        view = self.cache.get(key)
        if view is None:
            if len(self.cache) >= MAX_WORKSPACES:
                self.cache.pop(next(iter(self.cache))).close()
            view = self.cache[key] = self.objects_for(session)
        view.bind(session)
        return view

    def submit(self, reader: Reader, job: Callable[[Any], Any], *, priority: int = 0
               ) -> Future:
        return submit(self.jobs, self.stopped, reader, job, priority)

    def run(self, reader: Reader, job: Callable[[Any], Any], timeout: float = 120,
            priority: int = 0) -> Any:
        future = self.submit(reader, job, priority=priority)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("workbench_busy") from None

    def loop(self, until: Callable[[], bool] = lambda: False) -> None:
        """Run jobs on this (the owner) thread until ``stop`` or ``until()``."""
        try:
            while not self.stopped.is_set() and not until():
                try:
                    _, _, item = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    self.jobs.put((-1, 0, None))   # wake the next worker on this queue
                    break
                future, reader, job = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(job(self._workspace(reader.session)))
                except BaseException as error:  # noqa: BLE001 - handed to the route
                    future.set_exception(error)
        finally:
            self.stopped.set()
            while self.cache:
                self.cache.pop(next(iter(self.cache))).close()

    def stop(self) -> None:
        self.stopped.set()
        self.jobs.put((-1, 0, None))


_SEQ = iter(range(1, 1 << 62))
_SEQ_LOCK = threading.Lock()


def submit(jobs: "queue.PriorityQueue", stopped: threading.Event, reader: Reader,
           job: Callable[[Any], Any], priority: int) -> Future:
    future: Future = Future()
    if stopped.is_set():
        future.set_exception(LookupError("backend_stopped"))
        return future
    with _SEQ_LOCK:
        seq = next(_SEQ)
    jobs.put((priority, seq, (future, reader, job)))
    return future


class _Watched:
    """The workspace's access with the agent server's tool calls reported (status events);
    everything else is the access itself."""

    def __init__(self, access: Any, on_call: Callable[[str], None]) -> None:
        self._access, self._on_call = access, on_call

    def __getattr__(self, name: str) -> Any:
        return getattr(self._access, name)

    def agent_server(self) -> Any:
        server, on_call = self._access.agent_server(), self._on_call

        class Server:
            def __getattr__(self, name: str) -> Any:
                return getattr(server, name)

            def handle(self, message: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(message, Mapping) and message.get("method") == "tools/call":
                    on_call(str((message.get("params") or {}).get("name") or ""))
                return server.handle(message, *args, **kwargs)
        return Server()


# ---- the backend ---------------------------------------------------------------------

class WorkspaceBackend:
    """``objects_for``: the read worker's workspace factory (the owner thread's runtime).
    Asks run on the Ask pool (``add_ask_worker``: each worker its own runtime and thread,
    one shared bounded queue), so one Ask never holds up another reader's page."""

    def __init__(self, objects_for: Callable[[Any], Any], *, data_label: str,
                 state_dir: Optional[Path] = None, data_dir: Optional[Path] = None,
                 pg_dsn: Optional[str] = None, understand: Optional[Callable] = None,
                 search: Optional[Callable] = None, ask_served: bool = True,
                 today: Callable[[], date] | None = None) -> None:
        self.data_label = data_label
        self.state_dir = state_dir
        self.data_dir = Path(data_dir) if data_dir else None
        self.pg_dsn = pg_dsn
        self.understand = understand
        self.search = search
        self.ask_served = ask_served
        self.today = today
        self.reads = Worker(objects_for)
        self.ask_jobs: "queue.PriorityQueue" = queue.PriorityQueue()
        self.ask_stopped = threading.Event()
        self.ask_workers: list[Worker] = []
        self._answers: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, list[m.RecentQuestion]] = {}
        self._lock = threading.Lock()
        self._pool: Any = None
        self.page_cache: dict[tuple, tuple[float, Any]] = {}   # read worker only

    def add_ask_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """A worker of the Ask pool; the caller runs its ``loop`` on the thread that built
        ``objects_for``'s runtime."""
        worker = Worker(objects_for, self.ask_jobs)
        worker.stopped = self.ask_stopped
        self.ask_workers.append(worker)
        return worker

    def loop(self, until: Callable[[], bool] = lambda: False) -> None:
        """Serve page reads on the calling (owner) thread (``Worker``)."""
        self.reads.loop(until)

    def stop(self) -> None:
        self.reads.stop()
        self.ask_stopped.set()
        self.ask_jobs.put((-1, 0, None))
        if self._pool is not None:
            self._pool.close()

    def pg(self) -> Any:
        """the connection pool over ``pg_dsn`` (None: no Postgres configured)."""
        if not self.pg_dsn:
            return None
        if self._pool is None:
            from operating.pg.pool import Pool
            with self._lock:
                if self._pool is None:
                    self._pool = Pool(self.pg_dsn, max_size=4, timeout=2.0)
        return self._pool

    # ---- me ---------------------------------------------------------------------------

    def me(self, reader: Reader) -> m.Me:
        s = reader.session
        return m.Me(display_name=reader.display_name, email=reader.email,
                    principal=",".join(sorted(s.principals)), auth=reader.mode,
                    proof=s.proof, tenant=s.tenant, dev_mode=reader.mode != "entra",
                    data_label=self.data_label, expires_at=s.expires_at)

    # ---- freshness ----------------------------------------------------------------------

    def freshness(self, base: Mapping[str, Any] | None = None) -> m.Freshness:
        sources: list[m.FreshnessSource] = []
        oldest: Optional[str] = None
        ledger = self.state_dir / "host" / "host.sqlite" if self.state_dir else None
        if ledger is not None and ledger.is_file():
            try:
                conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=1)
                try:
                    rows = conn.execute("SELECT source, captured_at, stale_since FROM "
                                        "source_state WHERE configured = 1").fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                rows = []
            now = datetime.now(timezone.utc)
            seen: dict[str, m.FreshnessSource] = {}
            for source, captured, stale in rows:
                system = LEDGER_SYSTEMS.get(source)
                if system is None or not captured:
                    continue
                try:
                    age = int((now - datetime.fromisoformat(captured.replace("Z", "+00:00")))
                              .total_seconds())
                except ValueError:
                    continue
                held = seen.get(system)
                if held is None or (held.age_seconds or 0) < age:
                    seen[system] = m.FreshnessSource(system=system, as_of=captured,
                                                     age_seconds=max(0, age),
                                                     stale=bool(stale))
            sources = list(seen.values())
            oldest = min((s.as_of for s in sources if s.as_of), default=None)
        store = (base or {}).get("store") or {}
        built = store.get("built_at") if isinstance(store, Mapping) else None
        as_of = oldest or (built if isinstance(built, str) else None)
        label = None
        if as_of:
            try:
                moment = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                label = "Data as of " + moment.strftime("%d %b %Y %H:%M UTC").lstrip("0")
            except ValueError:
                label = f"Data as of {as_of}"
        return m.Freshness(as_of=as_of, label=label, sources=sources)

    # ---- sources ------------------------------------------------------------------------

    @staticmethod
    def _source(view: Any, handle: str, *, fields: Mapping[str, str] | None = None,
                quote_text: str | None = None, label: str | None = None,
                openable: bool | None = None) -> m.Source:
        if openable is None:
            openable = view.access.decide([handle]).get(handle) is None \
                and view._can_open(handle)
        return m.Source(system=system_of(handle), handle=handle,
                        label=label or record_words(handle), quote=quote_text,
                        fields=dict(fields) if fields else None,
                        open_url=open_url(handle) if openable else None)

    def _claim_sources(self, view: Any, claim: Mapping[str, Any]) -> list[m.Source]:
        """A gate-kept claim's sources with what the gate checked of each."""
        per: dict[str, dict[str, Any]] = {}
        for value in claim.get("value_claims") or ():
            if not isinstance(value, Mapping) or not isinstance(value.get("citation"), str):
                continue
            entry = per.setdefault(value["citation"], {"fields": {}, "quote": None})
            field = str(value.get("field") or "value").split(".")[-1].replace("_", " ")
            shown = plain(value.get("value"))
            if shown is not None:
                unit = value.get("unit") or value.get("currency")
                entry["fields"][field] = f"{shown} {unit}" if unit and str(unit) not in shown \
                    else shown
            if isinstance(value.get("quote"), str):
                entry["quote"] = value["quote"][:600]
        out = []
        for handle in list(dict.fromkeys(claim.get("citations") or ()))[:MAX_SOURCES_PER_FACT]:
            entry = per.get(handle, {})
            out.append(self._source(view, handle, fields=entry.get("fields") or None,
                                    quote_text=entry.get("quote")))
        return out

    # ---- ask ------------------------------------------------------------------------------

    def _understanding(self, view: Any, request: m.AskRequest,
                       found: Mapping[str, Any]) -> m.UnderstandingEvent:
        """the understanding when wired, its candidates re-checked against this
        reader's own read at this boundary (one it cannot open is dropped, unnamed);
        else the identifier grammar's roots the reader's ``plx.find`` resolved."""
        if self.understand is not None:
            try:
                got = understanding_event(self.understand(
                    request.question, history=[t.model_dump() for t in request.history],
                    actor=view.actor))
                return visible_understanding(view, got)
            except Exception:  # noqa: BLE001 - understanding is a hint; the loop still runs
                pass
        q = request.question.lower()
        intent = next((name for name, rule in INTENT_RULES if re.search(rule, q)), "other")
        entities = []
        for root in found.get("roots") or ():
            entities.append(m.UnderstoodEntity(
                text=str(root.get("mention") or root["object_id"])[:200],
                resolved=m.ObjectRef(object_id=root["object_id"],
                                     type=str(root.get("object_type") or "Object"),
                                     label=str(root.get("mention") or root["object_id"])[:200],
                                     open_url=object_url(root.get("object_type") or "Object",
                                                         root["object_id"]))))
        for item in found.get("ambiguous") or ():
            entities.append(m.UnderstoodEntity(
                text=str(item.get("mention"))[:200], ambiguous=True,
                candidates=[m.Candidate(object_id=c["object_id"],
                                        type=str(c.get("object_type") or "Object"),
                                        label=str(item.get("mention"))[:200])
                            for c in item.get("candidates") or () if c.get("object_id")]))
        return m.UnderstandingEvent(intent=intent, entities=entities)

    @staticmethod
    def _objective(request: m.AskRequest) -> str:
        """The question, with the person's earlier questions when it names no record of its
        own (a follow-up: "and the other one?"), so the loop's identifiers carry over."""
        from ontology.identifiers import find_identifiers
        from operating.objective_runner import MAX_OBJECTIVE

        question = request.question.strip()
        earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
                   and turn.text.strip()][-2:]
        if earlier and not list(find_identifiers(question)):
            context = " / ".join(earlier)
            text = f"{question}\n(Earlier in this conversation: {context})"
            return text[:MAX_OBJECTIVE]
        return question[:MAX_OBJECTIVE]

    def ask(self, reader: Reader, request: m.AskRequest) -> Iterator[tuple[str, Any]]:
        from operating import objective_runner
        from operator_ui import objective_box

        if not self.ask_served:
            raise errors.ASK_OFF
        if self.ask_jobs.qsize() >= MAX_QUEUED_ASKS:
            raise errors.ASK_BUSY
        started = time.perf_counter()

        def ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        conversation = request.conversation_id or secrets.token_hex(8)
        yield "status", m.StatusEvent(step="understanding", message="Reading your question",
                                      elapsed_ms=ms())
        objective = self._objective(request)
        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        def job(view: Any) -> m.AnswerPayload:
            if not view._ask_served():
                raise errors.ASK_OFF
            with view._request():
                found = objective_runner.find_roots(
                    objective, lambda arguments: view._call("plx.find", arguments))
                events.put(("understanding", self._understanding(view, request, found)))
            events.put(("status", m.StatusEvent(
                step="searching", message="Finding the records your question is about",
                elapsed_ms=ms())))

            def on_call(tool: str) -> None:
                events.put(("status", m.StatusEvent(
                    step="reading", message=STEP_WORDS.get(tool, "Reading records"),
                    elapsed_ms=ms())))
            access = view.access
            view.access = _Watched(access, on_call)
            try:
                if request.object_id:
                    out, ran = self._object_ask(view, request)
                else:
                    kwargs = {} if self.today is None else {"as_of": self.today()}
                    out, ran = objective_box.answer_run(view, {"objective": objective},
                                                        **kwargs)
                events.put(("status", m.StatusEvent(
                    step="checking", message="Checking every figure against its source",
                    elapsed_ms=ms())))
                with view._request():
                    return self._payload(view, request, out, ran, conversation, ms)
            finally:
                view.access = access

        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        future.add_done_callback(lambda _f: events.put(("done", None)))
        deadline = time.monotonic() + ASK_TIMEOUT_SECONDS
        while True:
            try:
                name, payload = events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                future.cancel()   # not started: never runs; running: its answer is dropped
                raise errors.ASK_TIMEOUT from None
            if name == "done":
                break
            yield name, payload
        answer = future.result()
        cited = {s.handle: s for f in answer.facts for s in f.sources}
        if cited:
            yield "sources", m.SourcesEvent(sources=list(cited.values()))
        yield "status", m.StatusEvent(step="answering", message="Writing the answer",
                                      elapsed_ms=ms())
        for word in re.findall(r"\S+\s*", answer.direct_answer):
            yield "token", m.TokenEvent(text=word)
        answer.latency_ms = ms()
        with self._lock:
            self._answers[answer.answer_id] = {"reader": reader.key,
                                               "question": request.question,
                                               "at": _iso_now()}
            while len(self._answers) > MAX_ANSWERS_KEPT:
                self._answers.pop(next(iter(self._answers)))
            recent = self._recent.setdefault(reader.key, [])
            recent.insert(0, m.RecentQuestion(question=request.question, asked_at=_iso_now(),
                                              answer_id=answer.answer_id,
                                              conversation_id=conversation))
            del recent[10:]
        yield "answer", answer

    @staticmethod
    def _object_ask(view: Any, request: m.AskRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        """The object page's Ask (``ObjectWorkspace.ask_run``) shaped as the objective box's
        run: the loop's unknowns stay as gaps (not found), each as the reply shows it."""
        from operator_ui.object_workspace import _final_reply

        shown = view.ask_run({"object_id": request.object_id, "question": request.question})
        reply = _final_reply(shown["claims"], shown["loop"], request.question)
        out = {**shown["base"],
               "answer": {"outcome": shown["outcome"], "reason": shown["reason"],
                          "text": reply["text"], "unknowns": reply["unknowns"]},
               "roots": [{"object_id": request.object_id}], "answers": [], "unfound": [],
               "ambiguous": [], "follow_ups": [],
               "gaps": [{"kind": "unknown", "subject": str(u), "state": "unknown",
                         "reason": "no record read answers it"}
                        for u in reply["unknowns"] or ()]}
        return out, {"claims": shown["claims"], "withheld": shown["withheld"],
                     "loop": shown["loop"]}

    def _payload(self, view: Any, request: m.AskRequest, out: Mapping[str, Any],
                 ran: Mapping[str, Any], conversation: str,
                 ms: Callable[[], int]) -> m.AnswerPayload:
        shown_answer = out.get("answer") or {}
        facts: list[m.Fact] = []
        for claim in ran.get("claims") or ():
            sources = self._claim_sources(view, claim)
            if sources:
                check = (claim.get("check") or {}).get("status")
                facts.append(m.Fact(text=str(claim.get("text") or "")[:1000], sources=sources,
                                    check=check if isinstance(check, str) else None))
        part_texts: list[str] = []
        not_found: list[m.NotFound] = []
        for root in out.get("answers") or ():
            for part in root.get("parts") or ():
                said = part.get("answer") or {}
                if said.get("state") not in (None, "not_established") \
                        and isinstance(said.get("text"), str):
                    part_texts.append(said["text"])
                for fact in said.get("facts") or ():
                    handle = fact.get("citation")
                    if isinstance(handle, str) and fact.get("openable") \
                            and len(facts) < MAX_FACTS:
                        facts.append(m.Fact(text=str(fact.get("text"))[:1000],
                                            sources=[self._source(view, handle,
                                                                  openable=True)],
                                            check="computed_from_cited_records"))
                for follow in said.get("follow_ups") or ():
                    not_found.append(missing_item(follow.get("missing"),
                                                  follow.get("who_can_answer"),
                                                  follow.get("why")))
        unfound = {str(item.get("mention")) for item in out.get("unfound") or ()}
        for gap in out.get("gaps") or ():
            if str(gap.get("subject")) in unfound:
                continue   # said once below, with where it would live
            follow = next((f["proposal"] for f in out.get("follow_ups") or ()
                           if (f.get("proposal") or {}).get("missing") == gap.get("subject")),
                          {})
            not_found.append(missing_item(gap.get("subject"), follow.get("who_can_answer"),
                                          gap.get("reason")))
        for item in out.get("unfound") or ():
            not_found.append(m.NotFound(
                what=f"A record for {str(item.get('mention'))[:120]}",
                where_it_would_live="No record with that reference is visible to you in "
                                    "Business Central, the Portal or captured mail and files"))
        not_found = dedupe_not_found(not_found)
        did_you_mean = [m.ObjectRef(object_id=c["object_id"],
                                    type=str(c.get("object_type") or "Object"),
                                    label=f"{item.get('mention')} ({c.get('object_type')})",
                                    open_url=object_url(c.get("object_type") or "Object",
                                                        c["object_id"]))
                        for item in out.get("ambiguous") or ()
                        for c in item.get("candidates") or () if c.get("object_id")]
        subjects = [m.ObjectRef(object_id=r["object_id"],
                                type=str(r.get("object_type") or "Object"),
                                label=str(r.get("mention") or r.get("object_type") or
                                          "Record")[:200],
                                open_url=object_url(r.get("object_type") or "Object",
                                                    r["object_id"]))
                    for r in out.get("roots") or () if isinstance(r.get("object_id"), str)]
        conflicts: list[m.Conflict] = []
        for root in subjects[:3]:
            card = view._call("plx.object", {"object_id": root.object_id})
            conflicts += self._conflicts(view, card.get("conflicts") or (), root.label)
        hidden = self._hidden(view, ran)
        outcome_loop = shown_answer.get("outcome")
        text = shown_answer.get("text") if outcome_loop == "answered" else None
        direct = (text or (part_texts[0] if part_texts else None)
                  or ("I found related records but could not confirm an answer from what "
                      "you can see." if facts else
                      "I couldn't find records you can see that answer this."))
        if facts and (not_found or outcome_loop != "answered"):
            outcome = "partial"
        elif facts:
            outcome = "answered"
        elif did_you_mean:
            outcome = "partial"
            direct = "That name or number matches more than one record. Which did you mean?"
        else:
            outcome = "not_found"
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
            direct_answer=str(direct)[:4000], facts=facts[:MAX_FACTS], not_found=not_found[:10],
            hidden=hidden, conflicts=conflicts[:6], freshness=self.freshness(out),
            subjects=subjects, did_you_mean=did_you_mean[:8], conversation_id=conversation,
            latency_ms=ms())

    def _units(self) -> dict[str, Any]:
        """``search.audience_units`` (label, people per unit); {} without
        Postgres, so every unit falls back to its family's rule."""
        pool = self.pg()
        if pool is None:
            return {}
        held = getattr(self, "_units_cache", None)
        if held is not None and held[0] > time.monotonic():
            return held[1]
        from operating.pg.permissions import units_in
        try:
            with pool.connection() as conn:
                units = units_in(conn)
        except Exception:  # noqa: BLE001 - no units table: family rules (fail closed)
            units = {}
        self._units_cache = (time.monotonic() + 60, units)
        return units

    def _hidden(self, view: Any, ran: Mapping[str, Any]) -> m.Hidden:
        """Claims the loop kept that cite a record this reader may not open, counted by
        the rule (``operating.pg.permissions``): an owner is named only for a shared
        unit or one the reader may open; other people's mail, chats and files are one
        unnamed bucket; a unit of an excluded person is never counted, nor a record whose
        unit cannot be told; no count at all while the exclusion list is not loaded.
        Nothing of a claim is shown."""
        from operating.pg.permissions import (PRIVATE_BUCKET, _label, is_excluded, is_shared,
                                              load_exclusion)
        from operator_ui.object_workspace import audience_unit

        exclusion = load_exclusion()
        if exclusion.unavailable:
            return m.Hidden(count=None, by_owner=[], message=None)
        shown = {id(c) for c in ran.get("claims") or ()}
        kept = (ran.get("loop") or {}).get("claims") or ()
        withheld = [c for c in kept if id(c) not in shown][:int(ran.get("withheld") or 0)]
        if not withheld:
            return m.Hidden(count=0, by_owner=[], message=None)
        units = self._units()
        gated, policy = view.access.reader_policy()
        named: dict[str, int] = {}
        unnamed = 0
        for claim in withheld:
            handles = [h for h in claim.get("citations") or () if isinstance(h, str)]
            decided = view.access.decide(handles) if handles else {}
            closed = [h for h in handles if decided.get(h) is not None] or handles
            key = audience_unit(closed[0]) if closed else None
            if key is None:
                continue   # its unit cannot be told: never counted (fail closed)
            unit = units.get(key)
            if is_excluded(key, unit, exclusion):
                continue
            try:
                readable = bool(policy.may_read(gated, key))
            except Exception:  # noqa: BLE001 - undecidable: not the reader's
                readable = False
            if readable or is_shared(key, unit):
                label = _label(key, unit)
                named[label] = named.get(label, 0) + 1
            else:
                unnamed += 1
        total = sum(named.values()) + unnamed
        if not total:
            return m.Hidden(count=0, by_owner=[], message=None)
        owners = [m.HiddenOwner(owner_or_team=label, count=n, named=True)
                  for label, n in sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))]
        if unnamed:
            owners.append(m.HiddenOwner(owner_or_team=PRIVATE_BUCKET, count=unnamed,
                                        named=False))
        return m.Hidden(count=total, by_owner=owners, message=hidden_message(total, owners))

    def _conflicts(self, view: Any, raw: Iterable[Any], subject: str) -> list[m.Conflict]:
        out = []
        for conflict in raw:
            if not isinstance(conflict, Mapping):
                continue
            values = [v for v in conflict.get("values") or () if isinstance(v, Mapping)
                      and isinstance((v.get("citation") or {}).get("l1_handle"), str)]
            handles = [v["citation"]["l1_handle"] for v in values]
            decided = view.access.decide(handles) if handles else {}
            visible = [v for v in values if decided.get(v["citation"]["l1_handle"]) is None]
            if len(visible) < 2:
                continue   # one side is not this reader's: no conflict card from it
            ranks = [v.get("authority_rank") for v in visible
                     if isinstance(v.get("authority_rank"), int)]
            best = min(ranks) if ranks else None
            winners = [v for v in visible if best is not None and v.get("authority_rank") == best]
            trusted_one = winners[0] if len(winners) == 1 else None
            field = str(conflict.get("subject") or "").split(".")[-1].replace("_", " ") \
                or "value"
            items = []
            for v in visible[:6]:
                handle = v["citation"]["l1_handle"]
                system = system_of(handle)
                if trusted_one is None:
                    why = ("No rule says which system governs this field, so both are shown; "
                           "check with the owner.")
                elif v is trusted_one:
                    why = f"{system} is the system of record for this field."
                else:
                    why = (f"{system_of(trusted_one['citation']['l1_handle'])} governs this "
                           f"field; this value disagrees with it.")
                items.append(m.ConflictValue(
                    value=plain(v.get("value")) or "(empty)",
                    source=self._source(view, handle, fields={field: plain(v.get("value"))
                                                              or "(empty)"}),
                    trusted=v is trusted_one, why=why))
            out.append(m.Conflict(field=field, subject=subject, values=items))
        return out

    # ---- suggest ----------------------------------------------------------------------

    def _reader_policy(self, reader: Reader) -> tuple[Any, Any]:
        """(gated actor, ``ReadPolicy``) of the reader now, from its own workspace on the
        read worker. Never cached here: the ``ScopeCache`` keys the scope on the
        policy's fingerprint and the exclusion, so a revoked grant changes the key at once."""
        def job(view: Any) -> tuple[Any, Any]:
            with view._request():
                return view.access.reader_policy()
        return self.reads.run(reader, job, timeout=10)

    def _scope(self, conn: Any, reader: Reader) -> Any:
        from operating.pg.permissions import reader_scope
        gated, policy = self._reader_policy(reader)
        return reader_scope(conn, policy, gated)

    def _pg_suggest(self, reader: Reader, q: str, limit: int) -> Optional[list[m.Suggestion]]:
        """``search.entities`` through the reader's scope (before ranking); None when Postgres holds no entities yet."""
        pool = self.pg()
        if pool is None:
            return None
        try:
            with pool.connection() as conn:
                if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.entities)"
                                ).fetchone()[0]:
                    return None
                scope = self._scope(conn, reader)
                if scope.empty:
                    return []
                conn.execute("SET LOCAL pg_trgm.similarity_threshold = 0.2")
                needle = normalize(q)
                rows = conn.execute(
                    "SELECT e.object_id, e.type, e.name, similarity(e.normalized, "
                    "%(q)s) AS s, e.normalized LIKE %(p)s AS prefix, e.normalized = %(q)s "
                    "AS exact FROM search.entities e WHERE " + scope.clause("e") +
                    " AND e.object_id IS NOT NULL AND (e.normalized %% %(q)s OR "
                    "e.normalized LIKE %(p)s) ORDER BY exact DESC, prefix DESC, s DESC, "
                    "e.name LIMIT %(n)s",
                    scope.params(q=needle, p=needle.replace("%", "").replace("_", "") + "%",
                                 n=limit * 3)).fetchall()
        except (TimeoutError, LookupError):
            raise
        except Exception:  # noqa: BLE001 - Postgres down: the projection answers
            return None
        out, seen = [], set()
        for object_id, kind, name, score, prefix, exact in rows:
            if object_id in seen:
                continue
            seen.add(object_id)
            out.append(m.Suggestion(object_id=object_id, type=kind or "Object",
                                    label=str(name or object_id)[:200], sublabel=kind,
                                    match="exact" if exact else "prefix" if prefix else "fuzzy",
                                    score=round(float(score or 0), 3),
                                    open_url=object_url(kind or "Object", object_id)))
        return out[:limit]

    def suggest(self, reader: Reader, q: str, limit: int) -> m.SuggestResponse:
        started = time.perf_counter()
        found = self._pg_suggest(reader, q, limit)
        source = "search_entities"
        if found is None:
            source = "projection"
            found = self.reads.run(reader, lambda view: projection_suggest(view, q, limit),
                                   timeout=READ_TIMEOUT_SECONDS)
        return m.SuggestResponse(query=q, suggestions=found, source=source,
                                 took_ms=int((time.perf_counter() - started) * 1000))

    # ---- home ---------------------------------------------------------------------------

    def home(self, reader: Reader) -> m.Home:
        def job(view: Any) -> tuple[dict[str, Any], dict[str, str]]:
            inbox = view.gap_inbox()
            labels: dict[str, str] = {}
            with view._request():
                for gap in (inbox.get("gaps") or ())[:20]:
                    oid = gap.get("object_id")
                    if isinstance(oid, str) and oid not in labels:
                        card = view._call("plx.object", {"object_id": oid})
                        labels[oid] = plain((card.get("object") or {}).get("display")) or ""
            return inbox, labels
        inbox, labels = self.reads.run(reader, job, timeout=READ_TIMEOUT_SECONDS)
        late: list[m.LatePo] = []
        risk: list[m.AtRisk] = []
        for gap in inbox.get("gaps") or ():
            oid, kind = gap.get("object_id"), str(gap.get("object_type") or "Object")
            if not isinstance(oid, str):
                continue
            label = labels.get(oid) or f"{kind} record"
            sources = [m.Source(system=system_of(h), handle=h, label=record_words(h),
                                open_url=open_url(h))
                       for h in (gap.get("citations") or ())[:3] if isinstance(h, str)]
            reason = str(gap.get("summary") or (gap.get("follow_up") or {}).get("why")
                         or "Needs attention")[:300]
            if gap.get("days_late") is not None and len(late) < 10:
                late.append(m.LatePo(object_id=oid, label=label, days_late=int(gap["days_late"]),
                                     reason=reason, open_url=object_url(kind, oid),
                                     sources=sources))
            elif gap.get("days_late") is None and len(risk) < 10:
                risk.append(m.AtRisk(object_id=oid, label=label, kind=str(gap.get("kind")),
                                     reason=reason, open_url=object_url(kind, oid),
                                     sources=sources))
        status = inbox.get("status")
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        return m.Home(
            greeting_name=reader.display_name.split(" ")[0],
            examples=["Which purchase orders are late?", "Why is our latest PO late?",
                      "What did we last tell our biggest customer?",
                      "Landed cost of a purchase order"],
            needs_attention=m.NeedsAttention(
                status="read" if status == "read" else "not_configured"
                if status in ("not_configured", "not_scheduled") else "unavailable",
                late_pos=late, at_risk=risk, hidden=None,   # the inbox counts no hidden gap
                as_of=inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None),
            recent_questions=recent, freshness=self.freshness(inbox))

    # ---- object page ---------------------------------------------------------------------

    def object_page(self, reader: Reader, object_type: str, object_id: str) -> m.ObjectPage:
        return self.reads.run(reader, lambda view: object_page(self, view, object_type,
                                                               object_id),
                              timeout=READ_TIMEOUT_SECONDS)

    def source(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        return self.reads.run(reader, lambda view: source_view(view, handle, text_offset),
                              timeout=READ_TIMEOUT_SECONDS)

    # ---- feedback -------------------------------------------------------------------------

    def feedback(self, reader: Reader, request: m.FeedbackRequest) -> m.FeedbackResponse:
        with self._lock:
            asked = self._answers.get(request.answer_id)
        if asked is None or asked["reader"] != reader.key:
            raise KeyError("answer_not_found")   # another reader's answer reads as none
        entry = {"at": _iso_now(), "actor": reader.key, "answer_id": request.answer_id,
                 "rating": request.rating, "note": request.note,
                 "question": asked["question"]}
        stored: list[str] = []
        feedback_id = secrets.token_hex(8)
        if self.pg_dsn:
            try:
                import psycopg
                with psycopg.connect(self.pg_dsn, connect_timeout=2, autocommit=True) as conn:
                    row = conn.execute(
                        "INSERT INTO search.feedback (at, actor, question, answer_id, rating, "
                        "note) VALUES (now(), %s, %s, %s, %s, %s) RETURNING id",
                        (entry["actor"], entry["question"], entry["answer_id"],
                         entry["rating"], entry["note"])).fetchone()
                    feedback_id = f"pg-{row[0]}"
                    stored.append("search.feedback")
            except Exception:  # noqa: BLE001 - the log below still keeps it
                pass
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.data_dir / "feedback.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({**entry, "feedback_id": feedback_id},
                                        ensure_ascii=False) + "\n")
            stored.append("feedback_log")
        if not stored:
            raise RuntimeError("feedback_not_stored")
        return m.FeedbackResponse(ok=True, feedback_id=feedback_id, stored_in=stored)


# ---- helpers shared by the routes ------------------------------------------------------

def hidden_message(total: int, owners: Sequence[m.HiddenOwner]) -> str:
    head = f"{total} more item{'s' if total != 1 else ''} match, but you don't have access."
    names = [o.owner_or_team for o in owners if o.named]
    if names:
        return f"{head} Ask the owners of {names[0]}."
    return f"{head} They are in other people's mail, chats or files."


def hidden_from_counts(counts: Mapping[str, Any]) -> m.Hidden:
    """the ``hidden_counts`` result as the payload's notice."""
    if counts.get("count") is None:
        return m.Hidden(count=None, by_owner=[], message=None)
    owners = [m.HiddenOwner(owner_or_team=str(o["owner_or_team"]), count=int(o["count"]),
                            named=bool(o.get("named", True)))
              for o in counts.get("by_owner") or () if int(o.get("count") or 0) > 0]
    total = int(counts["count"])
    return m.Hidden(count=total, by_owner=owners,
                    message=hidden_message(total, owners) if total else None)


def visible_understanding(view: Any, event: m.UnderstandingEvent) -> m.UnderstandingEvent:
    """Every candidate and resolution re-read by this reader (``plx.object``): one it
    cannot open leaves the event, unnamed and uncounted."""
    seen: dict[str, bool] = {}

    def visible(object_id: str) -> bool:
        if object_id not in seen:
            card = view._call("plx.object", {"object_id": object_id})
            seen[object_id] = not card.get("error")
        return seen[object_id]
    for entity in event.entities:   # inside the caller's request (one grant)
        entity.candidates = [c for c in entity.candidates[:8] if visible(c.object_id)]
        if entity.resolved is not None and not visible(entity.resolved.object_id):
            entity.resolved = None
    return event


def normalize(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", folded.lower())).strip()


def missing_item(what: Any, who: Any, why: Any) -> m.NotFound:
    team = teams(who or ())
    where = f"Ask {team}" if team else "Not on any record you can see"
    if isinstance(why, str) and why and not re.fullmatch(r"[a-z_]+", why):
        where = f"{where}. {why}"[:400]
    return m.NotFound(what=str(what or "An answer")[:300], where_it_would_live=where)


def dedupe_not_found(items: Sequence[m.NotFound]) -> list[m.NotFound]:
    seen, out = set(), []
    for item in items:
        if item.what in seen:
            continue
        seen.add(item.what)
        out.append(item)
    return out


def understanding_event(got: Any) -> m.UnderstandingEvent:
    """the ``Understanding`` (object or mapping) as the event."""
    def get(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    intent = get(got, "intent", "other")
    intents = m.UnderstandingEvent.model_fields["intent"].annotation.__args__
    entities = []
    for entity in get(got, "entities", []) or []:
        cands = [m.Candidate(object_id=str(get(c, "object_id")), type=str(get(c, "type") or
                                                                            "Object"),
                             label=str(get(c, "label") or get(c, "object_id"))[:200],
                             score=get(c, "score"))
                 for c in get(entity, "candidates", []) or [] if get(c, "object_id")]
        resolved = get(entity, "resolved")
        ref = next((c for c in cands if c.object_id == resolved), None)
        entities.append(m.UnderstoodEntity(
            text=str(get(entity, "text") or "")[:200],
            resolved=m.ObjectRef(object_id=ref.object_id, type=ref.type, label=ref.label,
                                 open_url=object_url(ref.type, ref.object_id)) if ref else None,
            ambiguous=bool(get(entity, "ambiguous", False)), candidates=cands[:8]))
    return m.UnderstandingEvent(intent=intent if intent in intents else "other",
                                entities=entities,
                                time_frame=str(get(got, "time_frame"))[:200]
                                if get(got, "time_frame") else None,
                                rewrites=[str(r)[:300] for r in get(got, "rewrites", []) or []][:5])


def projection_suggest(view: Any, q: str, limit: int) -> list[m.Suggestion]:
    """``plx.find`` for the reader (its grants, its ranking); typo tolerance by retrying
    shorter prefixes of the last word and dropping one word at a time."""
    from difflib import SequenceMatcher

    needle = q.strip()
    tries = [needle]
    words = needle.split()
    if words and len(words[-1]) > 3:
        tries += [" ".join(words[:-1] + [words[-1][:n]]) for n in
                  range(len(words[-1]) - 1, 2, -1)]
    if len(words) > 1:
        tries += [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
    out: dict[str, m.Suggestion] = {}
    with view._request():
        for attempt in tries[:6]:
            if len(out) >= limit:
                break
            result = view._call("plx.find", {"query": attempt[:200], "limit": limit})
            for cand in result.get("candidates") or ():
                obj = cand.get("object") or {}
                oid, kind = obj.get("object_id"), str(obj.get("object_type") or "Object")
                if not isinstance(oid, str) or oid in out:
                    continue
                label = plain(obj.get("display")) or oid
                ratio = SequenceMatcher(None, normalize(needle), normalize(label)).ratio()
                match = ("exact" if normalize(label) == normalize(needle) else
                         "prefix" if attempt == needle else "fuzzy")
                out[oid] = m.Suggestion(object_id=oid, type=kind, label=label[:200],
                                        sublabel=kind, match=match,
                                        score=round(max(ratio, 0.5 if match != "fuzzy"
                                                        else ratio), 3),
                                        open_url=object_url(kind, oid))
    ranked = sorted(out.values(), key=lambda s: ({"exact": 0, "prefix": 1, "fuzzy": 2}[s.match],
                                                -s.score, s.label))
    return ranked[:limit]


def resolve_object(view: Any, object_type: str, object_id: str) -> str:
    """The object id a page asks for: given whole (``<Type>:...``), else the one record of
    that type a business reference finds for the reader; several or none: not found."""
    if ":" in object_id and object_id.split(":", 1)[0].lower() == object_type.lower():
        return object_id
    result = view._call("plx.find", {"query": object_id[:200], "object_type": object_type,
                                     "limit": 3})
    if (result.get("error") or {}).get("code") == "invalid_input":
        result = view._call("plx.find", {"query": object_id[:200], "limit": 3})
    found = [c["object"]["object_id"] for c in result.get("candidates") or ()
             if isinstance((c.get("object") or {}).get("object_id"), str)
             and str(c["object"].get("object_type", "")).lower() == object_type.lower()]
    if len(found) != 1:
        raise KeyError("object_not_found")   # never merged: two matches are no page
    return found[0]


PAGE_CACHE_SECONDS = 60


def page_key(view: Any, oid: str) -> Optional[tuple]:
    """What a cached page is valid for: the record, the projection generation answering,
    and the reader's policy now (its fingerprint)
    with the people exclusion it was built under. None: never cached."""
    from operating.pg.permissions import _fingerprint, load_exclusion
    try:
        gated, policy = view.access.reader_policy()
        meta = view.access.store_meta() or {}
        return (oid, meta.get("content_digest"), _fingerprint(policy, gated),
                id(load_exclusion()))
    except Exception:  # noqa: BLE001 - an undecidable key is no cache entry
        return None


def object_page(backend: WorkspaceBackend, view: Any, object_type: str,
                object_id: str) -> m.ObjectPage:
    cache = backend.page_cache
    with view._request():
        ref = page_key(view, f"ref:{object_type}:{object_id}")
        held = cache.get(ref) if ref is not None else None
        if held is not None and held[0] > time.monotonic():
            oid = held[1]
        else:
            try:
                oid = resolve_object(view, object_type, object_id)
            except KeyError:
                oid = None
            if ref is not None:
                remember(cache, ref, oid)
        if oid is None:
            raise KeyError("object_not_found")   # resolved to nothing for this reader
        key = page_key(view, oid)
    held = cache.get(key) if key is not None else None
    if held is not None and held[0] > time.monotonic():
        with view._request():
            # item-level permissions (a SharePoint ACL, a mailbox) are not in the policy
            # fingerprint: every source the cached page shows is decided again now
            handles = sorted(page_handles(held[1]))
            decided = view.access.decide(handles) if handles else {}
        if all(decided.get(h) is None for h in handles):
            return held[1].model_copy(deep=True)
        cache.pop(key, None)
    page = view.workspace(oid)   # its own request; KeyError when not visible
    kind = str(((page.get("card") or {}).get("object") or {}).get("object_type") or "")
    plans = plan_runs(view, kind, oid)
    with view._request():
        model = _page_model(backend, view, page)
        model = with_plans(backend, view, model, plans)
        model.hidden = page_hidden(backend, view, oid)
    if key is not None:
        remember(cache, key, model.model_copy(deep=True))
    return model


def page_handles(value: Any) -> set[str]:
    """Every source handle a page payload shows."""
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, m.Source):
            found.add(item.handle)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif hasattr(item, "model_fields"):
            for name in type(item).model_fields:
                walk(getattr(item, name))
    walk(value)
    return found


def remember(cache: dict, key: tuple, value: Any) -> None:
    while len(cache) >= 512:
        cache.pop(next(iter(cache)))
    cache[key] = (time.monotonic() + PAGE_CACHE_SECONDS, value)


PAGE_PLANS = ("late_po", "landed_cost", "people_on_record")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


def plan_runs(view: Any, kind: str, oid: str) -> dict[str, Mapping[str, Any]]:
    """The record page's plan runs (late PO, landed cost, people on the record) as the
    console's investigate route returns them to this reader: the plan's structured
    ``investigation`` and its ``verdict``. A plan that fails is left out (fails closed)."""
    from operator_ui.object_workspace import plans_for

    families = {plan["family"] for plan in plans_for(kind)} if kind else set()
    out: dict[str, Mapping[str, Any]] = {}
    for family in PAGE_PLANS:
        if family not in families:
            continue
        try:
            ran = view.investigate(family, oid)
        except Exception:  # noqa: BLE001 - one plan's failure never costs the page
            continue
        if isinstance(ran, Mapping):
            out[family] = ran
    return out


def _entries(ran: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    answer = (ran.get("investigation") or {}).get("answer")
    return [e for e in answer if isinstance(e, Mapping)] if isinstance(answer, list) else []


def money_of(entry: Mapping[str, Any]) -> Optional[m.Money]:
    """A computed entry's amount as the plan computed it: its ``value`` (a decimal string,
    full precision) in its ``currency`` or ``unit`` when that is a currency code; a
    per-unit amount (``USD/ea``), a quantity or text is no money (None)."""
    value = entry.get("value")
    currency = entry.get("currency") if isinstance(entry.get("currency"), str) \
        else entry.get("unit")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()) \
            or not isinstance(currency, str) or not _CURRENCY.match(currency):
        return None
    if entry.get("unit") not in (None, currency):
        return None   # a currency beside another unit: a rate, never a total
    return m.Money(amount=value.strip(), currency=currency)


def entry_sources(backend: "WorkspaceBackend", view: Any, entry: Mapping[str, Any],
                  by_name: Mapping[str, Mapping[str, Any]], depth: int = 0) -> list[m.Source]:
    """The cited records a computed entry rests on: its own citation, else its inputs'
    (each re-decided for this reader; one they may not open is left out)."""
    handles: list[str] = []
    own = (entry.get("citation") or {}).get("l1_handle") \
        if isinstance(entry.get("citation"), Mapping) else None
    if isinstance(own, str):
        handles.append(own)
    elif depth < 3:
        for name in entry.get("inputs") or ():
            inner = by_name.get(name)
            if inner is not None:
                handles += [s.handle for s in entry_sources(backend, view, inner, by_name,
                                                            depth + 1)]
    handles = list(dict.fromkeys(handles))[:MAX_SOURCES_PER_FACT]
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None]


def landed_cost_of(backend: "WorkspaceBackend", view: Any,
                   ran: Mapping[str, Any]) -> m.LandedCost:
    """The landed-cost plan's own computed entries (``operating.landed_cost`` through
    ``plx.investigate``): ``landed_cost`` the total, ``landed_cost_line_<n>`` the lines,
    never a figure read out of prose. No computed total: not established."""
    entries = _entries(ran)
    by_name = {str(e.get("name")): e for e in entries}
    computed = {n: e for n, e in by_name.items() if e.get("kind") == "computed"}
    total_entry = computed.get("landed_cost")
    total = money_of(total_entry) if total_entry is not None else None
    lines = []
    for name in sorted((n for n in computed if re.fullmatch(r"landed_cost_line_\d+", n)),
                       key=lambda n: int(n.rsplit("_", 1)[1])):
        amount = money_of(computed[name])
        if amount is not None:
            lines.append(m.LandedCostLine(
                label=f"Line {name.rsplit('_', 1)[1]}", amount=amount,
                sources=entry_sources(backend, view, computed[name], by_name)))
    verdict = ran.get("verdict") or {}
    answerability = ((ran.get("investigation") or {}).get("answerability") or {}).get("state")
    status = ("computed" if total is not None and answerability == "answerable"
              else "partial" if total is not None or lines else "not_established")
    return m.LandedCost(status=status, total=total, lines=lines[:20],
                        missing=[str(f.get("missing"))[:300]
                                 for f in verdict.get("follow_ups") or ()
                                 if f.get("missing")][:10])


def people_of(backend: "WorkspaceBackend", view: Any, ran: Mapping[str, Any]
              ) -> Optional[list[m.Person]]:
    """The people plan's people (``operating.people_graph``: excluded people never reach
    the answer), each named by the label its cited entry holds and the roles the engine
    recorded; one whose citation the reader may not open is left out. None when the plan
    gave no person entries."""
    from operating.people_graph import ROLE_WEIGHTS

    people = []
    for entry in _entries(ran):
        value = entry.get("value")
        steps = entry.get("steps") or []
        roles_step = next((s for s in steps if isinstance(s, str)
                           and s.startswith("roles: ")), None)
        if entry.get("kind") != "computed" or not isinstance(value, Mapping) \
                or roles_step is None:
            continue
        handle = (value.get("origin") or {}).get("citation")
        if not isinstance(handle, str) or view.access.decide([handle]).get(handle) is not None:
            continue
        roles = [part.split(" ", 1)[0] for part in roles_step[len("roles: "):].split(", ")]
        roles = [r for r in roles if r in ROLE_WEIGHTS]
        name = plain(value)
        if name and roles:
            people.append(m.Person(name=name[:120], role=", ".join(roles),
                                   sources=[backend._source(view, handle)]))
    return people[:20] or None


def page_hidden(backend: "WorkspaceBackend", view: Any, oid: str) -> Optional[m.Hidden]:
    """the ``hidden_counts`` of the search chunks about this record the reader may
    not see; None (the section is omitted) without Postgres or chunks."""
    pool = backend.pg()
    if pool is None:
        return None
    from operating.pg.permissions import hidden_counts, reader_scope
    try:
        with pool.connection() as conn:
            if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.chunks)").fetchone()[0]:
                return None
            gated, policy = view.access.reader_policy()
            scope = reader_scope(conn, policy, gated)
            return hidden_from_counts(hidden_counts(conn, scope, object_ids=[oid]))
    except Exception:  # noqa: BLE001 - no count rather than a wrong one
        return None


def with_plans(backend: "WorkspaceBackend", view: Any, page: m.ObjectPage,
               plans: Mapping[str, Mapping[str, Any]]) -> m.ObjectPage:
    late = (plans.get("late_po") or {}).get("verdict")
    if isinstance(late, Mapping) and late.get("state") not in (None, "not_established"):
        cited = [(str(f.get("text") or "")[:300], f["citation"])
                 for f in late.get("facts") or ()
                 if isinstance(f.get("citation"), str) and f.get("openable")]
        page.late_lines = [m.LateLine(line=text, sources=[backend._source(view, h)])
                           for text, h in cited][:20]
        page.status = m.ObjectStatus(state=str(late.get("state")),
                                     text=str(late.get("text"))[:600],
                                     sources=[backend._source(view, h) for _, h in cited][:4])
    if "landed_cost" in plans:
        page.landed_cost = landed_cost_of(backend, view, plans["landed_cost"])
    if "people_on_record" in plans:
        page.people = people_of(backend, view, plans["people_on_record"])
    return page


def _page_model(backend: WorkspaceBackend, view: Any, page: Mapping[str, Any]) -> m.ObjectPage:
    card = page.get("card") or {}
    obj = card.get("object") or {}
    oid, kind = page["object_id"], str(obj.get("object_type") or "Object")
    label = plain(obj.get("display")) or oid
    facts = []
    for value in card.get("values") or ():
        handle = (value.get("citation") or {}).get("l1_handle")
        shown = plain(value.get("value"))
        if not isinstance(handle, str) or shown is None:
            continue
        name = str(value.get("property") or "value").replace("_", " ")
        facts.append(m.Fact(text=f"{name.capitalize()}: {shown}",
                            sources=[backend._source(view, handle, fields={name: shown})],
                            check="read_from_record"))
        if len(facts) >= MAX_FACTS:
            break
    answers = page.get("answers") or {}
    verdicts = [a for key, a in answers.items() if isinstance(a, Mapping) and key != "subject"
                and isinstance(a.get("text"), str)]
    lead = next((a for a in verdicts if a.get("state") not in ("not_established",)), None) \
        or (verdicts[0] if verdicts else None)
    status = m.ObjectStatus(
        state=str((lead or {}).get("state") or page.get("status") or "read"),
        text=str((lead or {}).get("text") or "No status established from your records")[:600],
        sources=[backend._source(view, f["citation"], openable=bool(f.get("openable")))
                 for f in (lead or {}).get("facts") or ()
                 if isinstance(f.get("citation"), str)][:4])
    timeline = []
    for event in (page.get("timeline") or {}).get("events") or ():
        handle = (event.get("citation") or {}).get("l1_handle") \
            if isinstance(event.get("citation"), Mapping) else event.get("citation")
        when = event.get("at") or event.get("occurred_at") or event.get("time")
        title = str(event.get("event_type") or event.get("type") or "Event")
        title = title.split(".")[-1].replace("_", " ").capitalize()
        timeline.append(m.TimelineEvent(
            at=when if isinstance(when, str) else None,
            system=system_of(handle) if isinstance(handle, str) else "Portal",
            title=title[:200], source=backend._source(view, handle)
            if isinstance(handle, str) else None))
    linked = []
    documents = ((page.get("documents") or {}).get("result") or {}).get("documents") or ()
    for doc in documents[:20]:
        handle = doc.get("l1_handle") or (doc.get("citation") or {}).get("l1_handle") \
            if isinstance(doc, Mapping) else None
        if not isinstance(handle, str):
            continue
        system = system_of(handle)
        kind_word = {"Email": "email", "Teams": "teams"}.get(system, "document")
        title = plain(doc.get("title")) or plain(doc.get("subject")) or record_words(handle)
        linked.append(m.LinkedItem(kind=kind_word, label=str(title)[:200],
                                   at=doc.get("at") if isinstance(doc.get("at"), str) else None,
                                   source=backend._source(view, handle)))
    related = []
    for hop in (page.get("around") or {}).get("hops") or ():
        for other in (hop.get("result") or {}).get("objects") or ():
            if isinstance(other.get("object_id"), str) and len(related) < 20:
                related.append(m.ObjectRef(
                    object_id=other["object_id"], type=str(other.get("object_type") or
                                                          "Object"),
                    label=(plain(other.get("display")) or other["object_id"])[:200],
                    open_url=object_url(other.get("object_type") or "Object",
                                        other["object_id"])))
    not_found = []
    for answer in verdicts:
        for follow in answer.get("follow_ups") or ():
            not_found.append(missing_item(follow.get("missing"), follow.get("who_can_answer"),
                                          follow.get("why")))
    conflicts = backend._conflicts(view, page.get("conflicts") or (), label)
    summary = next((a["text"] for a in verdicts if a is not lead), None) or kind
    return m.ObjectPage(object_id=oid, type=kind, label=label[:200], summary=str(summary)[:600],
                        status=status, facts=facts, timeline=timeline[:50], linked=linked,
                        people=None, landed_cost=None, late_lines=[], related=related,
                        not_found=dedupe_not_found(not_found)[:10], conflicts=conflicts[:6],
                        hidden=None,
                        freshness=backend.freshness(page))


def source_view(view: Any, handle: str, text_offset: int) -> m.SourceView:
    got = view.source(handle, text_offset=text_offset)   # KeyError: not this reader's
    text = None
    for key in ("text", "body", "content"):
        if got.get(key) is not None:
            text = plain(got[key]) if not isinstance(got[key], str) else got[key]
            break
    row = got.get("row") or got.get("fields") or got.get("values")
    fields = None
    if isinstance(row, Mapping):
        fields = {str(k)[:80]: (plain(v) or "") for k, v in list(row.items())[:60]
                  if not isinstance(v, (list, dict)) or isinstance(v, Mapping)
                  and "untrusted_text" in v}
    page = got.get("page") or {}
    following = page.get("next_text_offset") if isinstance(page, Mapping) else None
    return m.SourceView(handle=handle, system=system_of(handle), label=record_words(handle),
                        kind="text" if text is not None and not fields else "record",
                        title=plain(got.get("title")) if got.get("title") else None,
                        text=text[:200_000] if isinstance(text, str) else None,
                        fields=fields or None,
                        as_of=got.get("captured_at") if isinstance(got.get("captured_at"),
                                                                  str) else None,
                        text_offset=text_offset,
                        next_offset=following if isinstance(following, int) else None,
                        changed_since=got.get("status") == "changed_since")


# ---- start-up --------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("real backend (--dev / --entra-resource)")
    group.add_argument("--state-dir", type=Path, help="the host's state directory")
    group.add_argument("--host-config", type=Path,
                       help="operating.host config: the console's own arguments are derived "
                            "from it (as `operating.host serve` starts the console)")
    group.add_argument("--console-arg", action="append", default=[],
                       help="one more argument for the console builder (repeatable)")
    group.add_argument("--ask", action="store_true",
                       help="serve Ask: the employee agent loop (PLX_ASK_PLANNER)")
    group.add_argument("--ask-workers", type=int, default=ASK_WORKERS,
                       help="Asks answered at once (each its own runtime and thread)")
    group.add_argument("--dev-ticket-login", action="store_true",
                       help="--dev over live state: dev-login takes a console session ticket "
                            "(python3 -m operator_ui.console_ticket mint)")
    group.add_argument("--dev-employee-oid", help="--dev over a dev state: this employee")
    group.add_argument("--dev-fixture-principal", action="append", default=[],
                       help="--dev over a fixture state: a fixture principal")
    group.add_argument("--app-data-dir", type=Path, default=DEFAULT_DATA_DIR,
                       help="the app's own files (feedback log); never the state directory")
    group.add_argument("--pg-dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"),
                       help="search.entities (suggest) and search.feedback")


def console_argv(parser: argparse.ArgumentParser, args: argparse.Namespace,
                 mode: str) -> list[str]:
    if args.state_dir is None:
        parser.error("--state-dir is required for the real backend")
    argv: list[str] = []
    if args.host_config is not None:
        from operating.host import Host, HostConfig, console_arguments
        try:
            host = Host(args.state_dir, HostConfig.from_file(args.host_config))
            argv = console_arguments(host, 0)
        except Exception as error:  # noqa: BLE001 - named, never a trace
            parser.error(f"--host-config is unusable: {type(error).__name__}")
    else:
        argv = ["--state-dir", str(args.state_dir)]
    if mode == "entra":
        argv += ["--entra-resource", args.entra_resource]
    if args.dev_fixture_principal:
        argv += ["--fixture"] + [a for p in args.dev_fixture_principal
                                 for a in ("--principal", p)]
    if args.ask:
        argv += ["--ask"]
    return argv + list(args.console_arg)


def run(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
        start: Callable[[Any, Any, dict[str, Any]], int], *, fixtures: Any = None,
        planner: Any = None) -> int:
    """Build the console's readers from its own arguments and checks
    (``case_console.main(serve_with=...)``), then ``start(backend, auth, auth_config)``."""
    from operator_ui import case_console

    if mode == "dev":
        chosen = [flag for flag, given in (
            ("--dev-ticket-login", args.dev_ticket_login),
            ("--dev-employee-oid", args.dev_employee_oid is not None),
            ("--dev-fixture-principal", bool(args.dev_fixture_principal))) if given]
        if len(chosen) != 1:
            parser.error("--dev needs exactly one of --dev-ticket-login, --dev-employee-oid, "
                         "--dev-fixture-principal (there is no default identity)")
    elif args.dev_ticket_login or args.dev_employee_oid or args.dev_fixture_principal:
        parser.error("--dev-* identities are for --dev only")

    argv = console_argv(parser, args, mode)

    def serve_with(ctx: Mapping[str, Any]) -> int:
        auth, auth_config = make_auth(parser, args, mode, ctx)
        label = (ctx.get("evidence") or {}).get("label") or "source-mirror captures " \
            "(live-unverified)"
        backend = WorkspaceBackend(ctx["objects_for"], data_label=str(label),
                                   state_dir=Path(ctx["state_dir"]),
                                   data_dir=args.app_data_dir, pg_dsn=args.pg_dsn,
                                   understand=optional_understanding(),
                                   search=optional_search(), ask_served=bool(args.ask))
        threads = [start_ask_worker(backend, argv, fixtures, planner, n)
                   for n in range(max(0, args.ask_workers) if args.ask else 0)]
        try:
            return start(backend, auth, auth_config)
        finally:
            backend.stop()
            for thread in threads:
                thread.join(timeout=30)

    return case_console.main(argv, fixtures=fixtures, planner=planner, serve_with=serve_with)


def start_ask_worker(backend: WorkspaceBackend, argv: list[str], fixtures: Any, planner: Any,
                     number: int) -> threading.Thread:
    """One worker of the Ask pool: the console's builder run again on its own thread (its
    own case runtime and stores, every check again), serving Asks from the shared queue."""
    import logging
    from operator_ui import case_console

    def serve_asks(ctx: Mapping[str, Any]) -> int:
        backend.add_ask_worker(ctx["objects_for"]).loop()
        return 0

    def run_stack() -> None:
        try:
            case_console.main(argv, fixtures=fixtures, planner=planner, serve_with=serve_asks)
        except BaseException as error:  # noqa: BLE001 - logged; the pool runs one short
            logging.getLogger("operator_api").error("ask worker %s did not start: %s",
                                                    number, type(error).__name__)
    thread = threading.Thread(target=run_stack, name=f"app-ask-{number}", daemon=True)
    thread.start()
    return thread


def make_auth(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
              ctx: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    from operating import session_identity
    from .auth import DevAuth, EntraAuth, configured_employee, ticket_identity

    resolver, session = ctx.get("resolver"), ctx.get("session")
    if mode == "entra":
        resource = ctx.get("entra_resource")
        if resource is None or resolver is None:
            parser.error("--entra-resource needs the live employee policy")
        redirect = os.environ.get("PLX_APP_REDIRECT_URI") or None
        guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        client_id = next((a for a in sorted(resource.audiences) if guid.match(a)), "")
        config = {"entra": {"tenant_id": resource.tenant_id, "client_id": client_id,
                            "authority": f"https://login.microsoftonline.com/"
                                         f"{resource.tenant_id}/v2.0",
                            "scopes": [resource.required_scope], "redirect_uri": redirect},
                  "sign_in_available": redirect is not None and bool(client_id),
                  "message": None if redirect else
                  "Microsoft sign-in is waiting for the app's redirect URI to be registered."}
        return EntraAuth(resource, resolver, ctx["replay_guard"]), config
    if args.dev_fixture_principal:
        if session is None:
            parser.error("--dev-fixture-principal needs a fixture state directory")
        return DevAuth(lambda: session, mode="dev"), {"sign_in_available": True}
    if resolver is None:
        parser.error("--dev with an employee identity needs --employee-policy (console args)")
    if args.dev_employee_oid:
        try:
            session_identity.bind_state_dir(ctx["state_dir"], fixture=False, dev=True)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid never opens live state: " + error.reason)
        try:
            identity = configured_employee(resolver, args.dev_employee_oid)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid refused: " + error.reason)
        return DevAuth(identity, mode="dev"), {"sign_in_available": True}
    return (DevAuth(ticket_identity=ticket_identity(
        resolver, session_identity.session_secret(os.environ), ctx["replay_guard"]),
        mode="dev"), {"sign_in_available": True,
                      "message": "Demo sign-in: paste the access link you were given."})


def optional_understanding() -> Optional[Callable]:
    """the ``understand`` when merged and switched on (PLX_APP_UNDERSTANDING=1)."""
    if os.environ.get("PLX_APP_UNDERSTANDING") != "1":
        return None
    try:
        from operating.query_understanding import understand
    except ImportError:
        return None
    return understand


def optional_search() -> Optional[Callable]:
    """the ``search`` when merged and switched on (PLX_APP_HYBRID_SEARCH=1)."""
    if os.environ.get("PLX_APP_HYBRID_SEARCH") != "1":
        return None
    try:
        from operating.search import search
    except ImportError:
        return None
    return search
