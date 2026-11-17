"""The workspace backend's Ask helpers: clarifying, memory, general knowledge, ready answers."""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import re
import secrets
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote

from operating import currency_display
from operating import general_answer

from . import ask_progress, errors, models as m
from .ask_drafts import draft_models
from .auth import Reader
from .serving import (
    _Watched, ASK_TIMEOUT_SECONDS, FIRST_PARTIAL, HOST_READ_SCHEMES, INTENT_RULES,
    MAX_ANSWERS_KEPT, MAX_SOURCES_PER_FACT, object_url, open_url, plain, READ_TIMEOUT_SECONDS,
    record_words, submit, system_of
)
from .answers import (
    ACCESS_CAVEAT, aged_to_today, at_risk_gaps, AT_RISK_KINDS, attention_items, claim_refusal,
    hidden_message, late_answer, late_rule_behind, named_late_pos, understanding_event,
    visible_understanding, without_tests
)

from .backend_common import KEPT_LATE_PAGE, MEMORY_DECIDE_SECONDS, RESUME_HOST_CHECKS
from .backend_run import _keep_found


class AskMixin:
    """Ask helpers: clarifying, memory, general knowledge and the ready answers."""

    # ---- sources ------------------------------------------------------------------------

    @staticmethod
    def _source(view: Any, handle: str, *, fields: Mapping[str, str] | None = None,
                quote_text: str | None = None, label: str | None = None,
                openable: bool | None = None) -> m.Source:
        if openable is None:
            openable = view.access.decide([handle]).get(handle) is None \
                and view._can_open(handle)
        # the field the fact cites opens first on the source page (``&field=``)
        cited = next(iter(fields), None) if fields else None
        return m.Source(system=system_of(handle), handle=handle,
                        label=label or record_words(handle), quote=quote_text,
                        fields=dict(fields) if fields else None,
                        open_url=(open_url(handle) + (f"&field={quote(str(cited)[:80], safe='')}"
                                                      if cited else ""))
                        if openable else None)

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
                if not unit and currency_display.is_unstated(value):
                    shown = shown if currency_display.NOT_STATED in shown \
                        else currency_display.amount_words(shown)
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
        """the understanding when wired, run under this reader's own read policy
        (``view.access.reader_policy``: the gated actor and its ``ReadPolicy``, so its
        entity index is the reader's; UNDERSTAND-001: called without one it resolved
        nothing), its candidates re-checked against this reader's own read at this
        boundary (one it cannot open is dropped, unnamed), and every root the reader's
        ``plx.find`` already resolved kept (``_keep_found``); else, or when it fails, the
        identifier grammar's roots alone."""
        base = self._found_understanding(request, found)
        if self.understand is not None:
            try:
                gated, policy = view.access.reader_policy()
                # a record of an item-gated source (a mail or Teams participant, a file
                # or chat name) is a candidate only when this reader's read opens it
                visible = getattr(view.access, "visible_objects", None)
                got = understanding_event(self.understand(
                    request.question, history=[t.model_dump() for t in request.history],
                    actor=gated, policy=policy,
                    item_check=visible if callable(visible) else None))
                return visible_understanding(view, _keep_found(got, base))
            except Exception:  # noqa: BLE001 - understanding is a hint; the loop still runs
                pass
        return base

    @staticmethod
    def _found_understanding(request: m.AskRequest,
                             found: Mapping[str, Any]) -> m.UnderstandingEvent:
        """The identifier grammar's roots the reader's ``plx.find`` resolved, as an
        understanding event."""
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

    def _clarify(self, reader: Reader, request: m.AskRequest, reading: Any,
                 early: Optional[tuple[str, Future, dict[str, Any]]], conversation: str
                 ) -> Optional[tuple[Optional[m.Clarify], Optional[m.Assumption],
                                     m.AskRequest]]:
        """The front door flagged a choice the question leaves open: the reader's own
        records of that kind (``clarify.gather`` through their view) and the decision
        (``Clarifier.decide``): (the question to ask back, or the assumption and the
        request reworded under it); None: answered as read (no clarifier, nothing to
        ask, or no decision in time)."""
        from operating import clarify as cl
        if self.clarifier is None or request.object_id or request.source:
            return None
        try:
            found = early[1].result(timeout=READ_TIMEOUT_SECONDS) if early else None
        except Exception:  # noqa: BLE001 - the candidates are then listed by kind
            found = None
        page = request.context.object_id if request.context is not None and getattr(
            request.context, "object_id", None) else None

        def read(view: Any) -> list[dict[str, Any]]:
            with view._request():
                return cl.gather(lambda name, args: view._call(name, dict(args)), found,
                                 reading.open.get("among"), page_object=page)
        try:
            candidates = submit(self.ask_jobs, self.ask_stopped, reader, read, 1).result(
                timeout=READ_TIMEOUT_SECONDS)
            decision = self.clarifier.decide(
                request.question, reading.open,
                conversation=[{"role": t.role, "text": t.text} for t in request.history],
                reader={"name": reader.display_name, "roles": self._reader_roles(reader)},
                page=page, candidates=candidates)
        except Exception:  # noqa: BLE001 - no decision: answered as read
            return None
        if decision is None or decision.kind == "answer":
            return None
        options = [m.ClarifyOption(
            label=c.label, detail=c.detail or None, object=m.ObjectRef(
                object_id=c.object_id, type=c.object_type or "Object", label=c.label,
                open_url=object_url(c.object_type or "Object", c.object_id))
            if c.object_id else None) for c in decision.options]
        if decision.kind == "ask":
            return m.Clarify(question=decision.question, options=options), None, request
        return None, m.Assumption(text=decision.assumption, options=options), \
            request.model_copy(update={"question": cl.understood(
                request.question, assumption=decision.assumption)})

    def _memory_decisions(self) -> Any:
        """The read policy's decisions on what memory cites, held per person
        (``memory.Decisions``), so a turn's memory read never waits for the policy."""
        held = getattr(self, "_memory_decided", None)
        if held is None:
            from .memory import Decisions

            def ask(reader: Reader, handles: list[str]) -> dict[str, Any]:
                def job(view: Any) -> dict[str, Any]:
                    with view._request():
                        return dict(view.access.decide(list(handles)))
                return self.reads.run(reader, job, timeout=MEMORY_DECIDE_SECONDS)
            held = self._memory_decided = Decisions(ask)
        return held

    def _memory_decide(self, reader: Reader, *, wait: bool = False
                       ) -> Callable[[list[str]], Mapping[str, Any]]:
        """Handles -> the read policy's decision for ``reader`` (None: may open), for what
        memory shows them: the held decisions (the rest asked in the background and
        withheld meanwhile), or asked now (``wait``: the tool, the review screen)."""
        return self._memory_decisions().decider(reader, wait=wait)

    def _memory_warm(self, reader: Reader) -> None:
        """Ask ahead, in the background, for the decisions the reader's memory will need."""
        memory = getattr(self, "memory", None)
        if memory is None or getattr(reader.session, "view_as", False):
            return

        def warm() -> None:
            try:
                self._memory_decisions().warm(reader, memory.handles(reader))
            except Exception:  # noqa: BLE001 - a warm-up never fails a request
                pass
        threading.Thread(target=warm, daemon=True, name="plx-memory-warm").start()

    def _memory_turn(self, reader: Reader, request: m.AskRequest, conversation: str,
                     roots: Sequence[Mapping[str, Any]]) -> Optional[tuple[str, Any]]:
        """The reader's memory for this Ask turn (``operator_api.memory``): the context
        before the task and the ``plx.memory`` tool; None without a memory store."""
        memory = getattr(self, "memory", None)
        if memory is None or getattr(reader.session, "view_as", False) \
                or getattr(reader.session, "canary", False):
            # memory is the person's own: never shown to someone viewing as them, never fed
            # to (or by) the Ask canary's service session
            return None
        try:   # the reader's own applied lessons into their memory (learning's hand-off)
            from operating import learning
            memory.submit(lambda: learning.sync_memory(memory, reader, self.state_dir))
        except ImportError:
            pass
        records = [str(r.get("object_id")) for r in roots if r.get("object_id")]
        return (memory.context(reader, request.question, conversation=conversation,
                               records=records, decide=self._memory_decide(reader)),
                memory.tool(reader, self._memory_decide(reader, wait=True)))

    def _clarified(self, reader: Reader, request: m.AskRequest) -> m.AskRequest:
        """A reply to the question an earlier answer asked back (or another reading of
        its assumption): that question again, with the record picked as ``object_id``
        or the person's words added. When the earlier answer is not this reader's own,
        no longer kept (a restart) or kept no question, the reply's own words are
        answered as they stand (never another reader's question or records), and never
        asked back again."""
        from operating import clarify as cl
        reply = request.clarified
        assert reply is not None
        with self._lock:
            kept = self._answers.get(reply.answer_id)
        if not kept or kept.get("reader") != reader.key or not kept.get("clarify"):
            return request
        held = kept["clarify"]
        if request.context is None and held.get("context"):
            # the page the question was asked on
            request = request.model_copy(update={
                "context": m.InstructContext.model_validate(held["context"])})
        memory = getattr(self, "memory", None)
        if memory is not None:
            choice = reply.text if reply.option is None else (
                held["options"][reply.option]["label"]
                if reply.option < len(held["options"]) else None)
            if choice:
                memory.observe("clarify", reader, held["question"],
                               str(held.get("asked") or ""), str(choice), reply.answer_id,
                               request.conversation_id)
        if reply.option is not None:
            if reply.option >= len(held["options"]):
                return request
            picked = held["options"][reply.option]
            return request.model_copy(update={
                "question": cl.understood(held["question"], choice=picked["label"]),
                "object_id": picked.get("object_id") or request.object_id})
        return request.model_copy(update={"question": cl.understood(
            held["question"], choice=reply.text, asked=held.get("asked") or "")})

    def _reader_roles(self, reader: Reader) -> list[str]:
        """The reader's roles in the roles file (``autopilot._roles``); [] when none."""
        try:
            employee = getattr(reader.session, "employee", None)
            oid = str(getattr(employee, "object_id", "") or "").lower()
            return sorted(role for role, members in self._apa_roles().items()
                          if oid and oid in members)
        except Exception:  # noqa: BLE001 - no roles known
            return []

    def _general_served(self) -> bool:
        """Whether general answers run here: a model the app reads questions with (the
        front door) or a test's stand-in; never a model call the app is not set up for."""
        return self.general_call is not None or self.front_door is not None

    def ask_withheld(self, reader: Reader, handles: list[str]) -> Optional[str]:
        """Why a kept answer is not shown to this reader now: a source it cites that the
        read policy withholds from them (``feedback_grant``); raises when undecidable. A
        captured mail original or SharePoint file (``HOST_READ_SCHEMES``) the store leaves
        to the reader's host is read through that host again (their mailbox and site
        access now); a host that does not open it, or no host, withholds the turn."""
        if not handles:
            return None
        from operator_api.workspace.object_workspace import ITEM_REFUSED
        _held, decided = self.feedback_grant(reader, handles)
        hosted: list[str] = []
        for handle in handles:
            marker = decided.get(handle, {"reason": "undecided"})
            if marker is None:
                continue
            if (handle.startswith(tuple(HOST_READ_SCHEMES)) and isinstance(marker, Mapping)
                    and marker.get("reason") in (ITEM_REFUSED, "item_refused")):
                hosted.append(handle)
                continue
            return "withheld"
        if len(hosted) > RESUME_HOST_CHECKS:
            return "withheld"      # too many to re-read: not decided, so not shown
        for handle in hosted:
            if not self._host_opens(reader, handle):
                return "withheld"
        return None

    def _host_opens(self, reader: Reader, handle: str) -> bool:
        """Whether the reader's own host opens this captured original or SharePoint file
        now (``AgentAsk.read_source``); any failure, or no host for them, is no."""
        if self.agent_ask is None or not self.agent_ask.serves(reader.session):
            return False
        try:
            with self._original_reads(reader):
                self.agent_ask.read_source(reader.session, handle, 0)
        except Exception:  # noqa: BLE001 - refused, unknown, busy or failed alike: no
            return False
        return True

    def _attached(self, request: m.AskRequest) -> Optional["ask_attachments.Batch"]:
        """The request's attachments as read (``operating.ask_attachments``), their text
        cleaned of excluded people's addresses and ids; None without attachments. With
        the exclusion list unavailable none is read (fail closed)."""
        if not request.attachments:
            return None
        from operating import ask_attachments
        batch = ask_attachments.decode(a.model_dump() for a in request.attachments)
        try:
            guard = self.people_guard()
        except Exception:  # noqa: BLE001 - undecidable: nothing read
            guard = None
        if guard is None or getattr(guard, "unavailable", "absent"):
            batch.files = [dataclasses.replace(
                f, text="", image=None, unread=f.unread or "Attachments can't be read right "
                "now: the list of people whose data stays out is not loaded.")
                for f in batch.files]
            return batch
        from .feedback import clean
        batch.files = [dataclasses.replace(f, text=clean(f.text, guard, len(f.text) + 1)
                                           or "") if f.text else f for f in batch.files]
        return batch

    @staticmethod
    def _general_wait(future: Future) -> Optional["general_answer.GeneralAnswer"]:
        try:
            got = future.result(timeout=general_answer.SECONDS + 5)
        except (FutureTimeout, Exception):  # noqa: BLE001 - no general part
            return None
        if got is not None:
            # counts only, never the question or the words
            logging.getLogger("operator_api.ask").info(json.dumps(
                {"general": True, "seconds": round(got.seconds, 1),
                 "removed_sentences": len(got.removed)}))
        return got

    def _general_ask(self, request: m.AskRequest, conversation: str, ms: Callable[[], int],
                     earlier: Sequence[str], *, files: Optional[Any] = None,
                     person: Optional[str] = None) -> Iterator[tuple[str, Any]]:
        """A question no record answers (``general_answer``): the model's general knowledge
        under ``general_answer.HEADING``, screened of company claims; no fact, source,
        citation or proposal. None when the model gave nothing."""
        read = files is not None and bool(files.read)
        yield "status", m.StatusEvent(step="answering", message="Answering from your "
                                      "attachment" if read else "Answering from general "
                                      "knowledge", elapsed_ms=ms())
        got = self._general_wait(self._general_start(request.question, earlier, "general",
                                                     files=files, person=person))
        if got is None:
            return None
        answer_id = secrets.token_hex(12)
        return m.AnswerPayload(
            answer_id=answer_id, question=request.question, outcome="answered",
            direct_answer=general_answer.combined("", got, heading=(
                general_answer.ATTACHMENT_HEADING if read else general_answer.HEADING)),
            hidden=m.Hidden(count=0, by_owner=[], message=None), freshness=m.Freshness(),
            conversation_id=conversation, latency_ms=ms(),
            drafts=draft_models(answer_id, got.drafts))

    def _with_general(self, answer: m.AnswerPayload, future: Future, *,
                      heading: str = general_answer.HEADING) -> m.AnswerPayload:
        """The evidence path's answer as it is (its facts, sources and outcome), its words
        followed by the general part under its heading, and the drafts it wrote."""
        got = self._general_wait(future)
        if got is None:
            return answer
        return answer.model_copy(update={
            "direct_answer": general_answer.combined(answer.direct_answer, got,
                                                     heading=heading),
            "drafts": [*answer.drafts, *map(m.AskDraft.model_validate, draft_models(
                answer.answer_id, got.drafts))]})

    def _first_notice(self, reader: Any, conversation: Any, notice: str) -> bool:
        """Whether ``notice`` is new in this reader's conversation (then it is kept as
        said): a data note is said once per conversation, not on every answer."""
        key = (reader, conversation)
        with self._lock:
            notices = self.__dict__.setdefault("_notices", {})
            said = notices.setdefault(key, set())
            if notice in said:
                return False
            said.add(notice)
            while len(notices) > MAX_ANSWERS_KEPT:
                notices.pop(next(iter(notices)))
            return True

    def _drain(self, events: "queue.Queue[tuple[str, Any]]", future: Future,
               seconds: float, cancel: Optional[threading.Event] = None
               ) -> Iterator[tuple[str, Any]]:
        """Yield ``events`` until ``future`` is done (it puts ``("done", None)``); past
        ``seconds`` the Ask times out (a queued job never runs; a running one is dropped).
        However the drain ends before ``done`` (a timeout, or the reader gone: the stream
        closed), the job is cancelled and ``cancel`` set, which ends the agent session
        still running for it (API-011)."""
        deadline = time.monotonic() + seconds
        finished = False
        try:
            while True:
                try:
                    name, payload = events.get(
                        timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    raise errors.ASK_TIMEOUT from None
                if name == "done":
                    finished = True
                    return
                yield name, payload
        finally:
            if not finished:
                future.cancel()
                if cancel is not None:
                    cancel.set()

    def _late_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                  ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """The late purchase orders the reader may see: the list Home shows (the gap
        inbox, trimmed for the reader), each cited by its gap's own evidence."""
        yield "status", m.StatusEvent(step="searching",
                                      message="Reading the late purchase orders you can see",
                                      elapsed_ms=ms())
        try:
            answer = self.reads.run(reader, self.late_job(request, conversation, ms),
                                    timeout=READ_TIMEOUT_SECONDS)
        except (TimeoutError, PermissionError):
            # its read queued past the limit behind other readers' reads (11-13 W9: four
            # asks at once, this one refused busy at 65 s with the session's whole budget
            # left): the Late page's own list for this reader when it holds one (the UX
            # walk of 11-13: the agent then timed out at 128 s while /late showed the
            # list), else the agent answers, as the other shortcuts' do
            return self._late_from_page(reader, request, conversation, ms)
        # Never a non-answer: no list known for the newest data and no last completed
        # one either goes on to a live read of the purchase orders (the agent path)
        return None if answer.outcome == "not_found" else answer

    def _late_from_page(self, reader: Reader, request: m.AskRequest, conversation: str,
                        ms: Callable[[], int]) -> Optional[m.AnswerPayload]:
        """The late-list answer from the Late page's own first page for this reader as the
        page is served (``late_list``: a page kept for them, decided for them again,
        marked stale when it is; never a first read here: ``first_partial``), each source
        decided for this reader again by ``late_answer`` on the Ask pool. None when no page
        is kept or it lists nothing (the agent answers)."""
        page = self._kept_late(reader)
        if page is None or page.reading or not page.items:
            return None
        # the page's own list, read under its as-of (or when a stale page was read)
        inbox = {"status": "read", "run_at": page.stale_since or page.as_of, "gaps": [],
                 "late_total": page.total}

        def job(view: Any) -> m.AnswerPayload:
            with view._request():
                return late_answer(self, view, request, inbox, page.items, conversation, ms)
        return self._ask_run(reader, job)

    def _kept_late(self, reader: Reader) -> Optional[m.LateList]:
        """The Late page's first page as it is served to ``reader`` now, never read on
        this thread (a page not kept answers ``reading``); None when refused."""
        from .serving import FIRST_PARTIAL
        token = FIRST_PARTIAL.set(True)   # as the app's request asks (``first_partial``)
        try:
            # the page the app's shell keeps current on every load (``getLateList`` 100)
            return self.late_list(reader, offset=0, limit=KEPT_LATE_PAGE)
        except Exception:  # noqa: BLE001 - busy or refused: the agent answers
            return None
        finally:
            FIRST_PARTIAL.reset(token)

    def _ask_run(self, reader: Reader, job: Callable[[Any], Any]) -> Any:
        """``job`` on the Ask pool for ``reader`` (never behind the read workers' queue),
        its result within ``READ_TIMEOUT_SECONDS``; None past it or when it fails."""
        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        try:
            return future.result(timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - not in time or failed: the agent answers
            future.cancel()
            return None

    def _late_fallback(self, view: Any) -> tuple[list[m.LatePo], int]:
        """The late orders the Late page holds for this reader (``_late_held``: its last
        completed read), each kept only when every source it cites opens for the reader
        now; and their count. ([], 0) when that read fails."""
        try:
            _inbox, ordered, labels, suppliers = self._late_held(view)
            with view._request():
                handles = sorted({h for g in ordered for h in g.get("citations") or ()
                                  if isinstance(h, str)})
                decided = view.access.decide(handles) if handles else {}
        except Exception:  # noqa: BLE001 - nothing to fall back to: the live read answers
            return [], 0
        # a handle the policy did not decide is withheld, and so is an order that cites
        # nothing: there is nothing to decide it by (fail closed)
        def opens(gap: Mapping[str, Any]) -> bool:
            cited = [h for h in gap.get("citations") or () if isinstance(h, str) and h]
            return bool(cited) and all(
                decided.get(h, {"reason": "undecided"}) is None for h in cited)
        shown = [g for g in ordered if opens(g)]
        late, _ = attention_items({"gaps": shown}, labels, suppliers=suppliers)
        return late, len(shown)

    def late_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int]
                 ) -> Callable[[Any], m.AnswerPayload]:
        """The late-list Ask's work on a reader's workspace (``view``): the job ``_late_ask``
        runs, and the scoreboard runs as the app's route (``scripts.eval_live_ops``)."""
        def job(view: Any) -> m.AnswerPayload:
            inbox, labels, suppliers = without_tests(*self._attention_cached(view),
                                                     named=self.test_named())
            inbox = aged_to_today(inbox, self.today() if self.today else None)
            late, _risk = attention_items(inbox, labels, suppliers=suppliers)
            if not late and late_rule_behind(inbox)[1]:
                # the check is re-running (or did not run) on the newest data: the last
                # completed result the Late page holds, decided for this reader now
                late, total = self._late_fallback(view)
                if late:
                    inbox = {**inbox, "late_total": total}
            chains = at_risk_gaps(view)
            if chains is not None:
                inbox = {**inbox, "gaps": [g for g in inbox.get("gaps") or ()
                                           if g.get("kind") not in AT_RISK_KINDS] + chains}
            with view._request():
                return late_answer(self, view, request, inbox, late, conversation, ms)
        return job

    def _at_risk_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                     ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        yield "status", m.StatusEvent(step="searching", message="Reading the customer "
                                      "orders late purchase orders hold up", elapsed_ms=ms())
        try:
            return self.reads.run(reader, self.at_risk_job(request, conversation, ms),
                                  timeout=READ_TIMEOUT_SECONDS)
        except (TimeoutError, PermissionError):
            return None   # the agent answers

    def _labour_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                    ms: Callable[[], int], question: str) -> m.AnswerPayload:
        """A labour-hours question (``operating.labour_hours.answer``): the aggregates the
        reader may see, or a decline; no model call."""
        from .briefs import labour_answer_of
        said = labour_answer_of(self.state_dir, reader, question)
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=request.question,
            outcome=said["outcome"],
            direct_answer=said["text"], hidden=m.Hidden(count=0, by_owner=[], message=None),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=ms())

    def _profit_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                    ms: Callable[[], int], *, question: Optional[str] = None
                    ) -> Iterator[tuple[str, Any]]:
        """Is a customer profitable, and why not (``operating.customer_profit``): the
        customer the question names, found by this reader's ``plx.find`` (or the customer
        record ``request.object_id`` names: a pick or the record's page), its orders'
        checked margins, the worst order's cost breakdown, the trail and a suggested next
        step. ``question``: the question being answered (a pick's earlier question; default
        the request's). None when it names no customer this reader can find (the agent path
        answers)."""
        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        job = self.profit_job(request, conversation, ms, question=question,
                              progress=events.put, reader=reader)
        if job is None:
            return None
        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        future.add_done_callback(lambda _f: events.put(("done", None)))
        yield from self._drain(events, future, ASK_TIMEOUT_SECONDS)
        return future.result()

    def _early_verdict(self, reader: Reader, request: m.AskRequest, pre: Mapping[str, Any],
                       text: str, cited: Sequence[str]) -> Optional[str]:
        """The gate-accepted first line (``ask_progress.verdict``) when this reader may
        see it now: every citation of the passed claims decided for this reader again as
        the answer decides them (``claim_refusal``), and the answer will not lead with
        something else (a PO the late list names leads with its lateness). None: the full
        answer comes as before."""
        def job(view: Any) -> Optional[str]:
            with view._request():
                decided = view.access.decide(list(cited))
                if not cited or set(cited) - set(decided) \
                        or claim_refusal(decided) is not None:
                    return None
                if named_late_pos(self, view, request.question, pre.get("found") or {}):
                    return None
                if ACCESS_CAVEAT.search(text):
                    return None
                return text
        try:
            return submit(self.ask_jobs, self.ask_stopped, reader, job, 0).result(
                timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - no early line; the answer still comes
            return None

    def _box_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                 ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """The console's objective box (``operator_api.workspace.objective_box``) for a reader the
        agent path does not serve."""
        from operating import objective_runner
        from operator_api.workspace import objective_box

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
                    step="reading", message=ask_progress.call_step(tool),
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
        yield from self._drain(events, future, ASK_TIMEOUT_SECONDS)
        return future.result()

    @staticmethod
    def _object_ask(view: Any, request: m.AskRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        """The object page's Ask (``ObjectWorkspace.ask_run``) shaped as the objective box's
        run: the loop's unknowns stay as gaps (not found), each as the reply shows it."""
        from operator_api.workspace.object_workspace import _final_reply

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
        from operator_api.workspace.object_workspace import audience_unit

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
        """The subject's conflict cards: ``operating.answer_conflicts`` decides (system of
        record, then newest, then most specific document, else none trusted; each value
        says why) over the values this reader may open (``view.access.decide``); a
        disagreement with one side hidden is no card."""
        from operating.answer_conflicts import tool_conflicts

        def visible(handles: Any) -> set:
            decided = view.access.decide(list(handles)) if handles else {}
            return {h for h in handles if decided.get(h) is None}

        out = []
        for found in tool_conflicts(raw, visible=visible, subject=subject):
            items = [m.ConflictValue(
                value=v["value"], trusted=v["trusted"], why=v["why"][:600],
                source=self._source(view, v["source"], fields={found["field"]: v["value"]}))
                for v in found["values"]]
            out.append(m.Conflict(field=found["field"], subject=subject, values=items))
        return out
