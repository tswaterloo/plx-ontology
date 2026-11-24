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



def _understood(question: str, *, assumption: str = "", choice: Optional[str] = None,
                asked: str = "") -> str:
    """The question the answer path works with: the person's words, then what they
    meant (their pick or their own answer to ``asked``) or the stated assumption."""
    question = question.strip()
    if choice:
        meant = f"Asked \"{asked}\", they answered: {choice}" if asked else \
            f"They mean: {choice}"
        return f"{question}\n({meant})"
    if assumption:
        return f"{question}\n(Read as: {assumption})"
    return question


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
                "question": _understood(held["question"], choice=picked["label"]),
                "object_id": picked.get("object_id") or request.object_id})
        return request.model_copy(update={"question": _understood(
            held["question"], choice=reply.text, asked=held.get("asked") or "")})



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

    def ask_uploads(self) -> Any:
        """The store of files streamed to ``POST /api/ask/files`` (``operator_api.ask_files``)."""
        from . import ask_files
        return ask_files.store(self)

    def _attached(self, request: m.AskRequest, reader: Optional[Reader] = None
                  ) -> Optional["ask_attachments.Batch"]:
        """The request's attachments as read (``operating.ask_attachments``), their text
        cleaned of excluded people's addresses and ids; None without attachments. With
        the exclusion list unavailable none is read (fail closed). With the ``reader``,
        each Word, Excel or PowerPoint file is kept as their own file to edit, and a PDF, CSV or
        old .xls/.doc file whose text was cut for the question as their own file to read on
        (``_keep_attached``); no other file's bytes outlive this call."""
        if not request.attachments:
            return None
        from operating import ask_attachments
        from . import ask_files
        batch = ask_attachments.decode(ask_files.items(self, request, reader))
        batch.files = ask_attachments.screened(batch.files)
        try:
            guard = self.people_guard()
        except Exception:  # noqa: BLE001 - undecidable: nothing read
            guard = None
        if guard is None or getattr(guard, "unavailable", "absent"):
            batch.files = [dataclasses.replace(
                f, text="", image=None, data=None, unread=f.unread or "Attachments can't be read right "
                "now: the list of people whose data stays out is not loaded.")
                for f in batch.files]
            return batch
        from .feedback import clean
        batch.files = [dataclasses.replace(f, text=clean(f.text, guard, len(f.text) + 1)
                                           or "") if f.text else f for f in batch.files]
        batch.files = [_attached_kept(self, reader, f, guard)
                       if f.data else
                       dataclasses.replace(f, data=None, whole=None) for f in batch.files]
        return batch

    def _ocr_rest(self, reader: Reader, citation: str) -> bool:
        """The rest of a kept scan read by OCR in the background (``ocr_background``);
        whether it is being read."""
        from . import ocr_background
        uploads = getattr(self, "_uploads", None)
        if not callable(uploads) or not citation.startswith("file:"):
            return False
        return ocr_background.start(uploads(), citation[len("file:"):]) in (
            "started", "running", "done")

    def _keep_attached(self, reader: Reader, f: "ask_attachments.Attached", guard: Any
                       ) -> Optional[str]:
        """An attached Word, Excel or PowerPoint file kept as the reader's own file (the
        made documents' store, ``instruct_files.UploadDesk``: theirs only, downloaded only
        while nobody it names is excluded, the same as the copies the Ask edits), so the
        Ask can edit it (``plx.edit_document`` with the ``file:`` citation answered) and
        hand the edited copy back. None, and nothing kept: a session not the person's own
        (viewing as someone, the canary), a file the assistant may not read whole
        (Freightcom or an excluded person named anywhere in it, or its text not readable
        whole), or no store."""
        from operating import ask_attachments, doc_edit
        from operating.instruct_files import file_name
        from .feedback import _ADDRESS
        session = getattr(reader, "session", None)
        kind = ask_attachments.EDITABLE.get(f.media_type or "")
        if kind is None and f.cut:
            # a file cut for the question, kept to be read to the end (not to be edited)
            kind = "pdf" if f.kind == "pdf" else "csv" \
                if (f.media_type or "").startswith("text/") else \
                (("xls" if f.kind == "spreadsheet" else "doc") if not f.media_type
                 else None)   # an old binary Excel or Word file: no media type, read by its bytes
        uploads = getattr(self, "_uploads", None)
        withheld = getattr(self, "_pw_withheld", None)
        if kind is None or not f.data or not callable(uploads) or not callable(withheld) \
                or getattr(session, "view_as", False) or getattr(session, "canary", False):
            return None
        name = file_name(f.name, kind)
        try:
            text = doc_edit.text_of(name, f.data) if kind in \
                ask_attachments.EDITABLE.values() else (f.whole or f.text)
            if withheld(" \n".join([name, text]), guard):
                return None
            row = uploads().prepare(reader.key, name=name, kind=kind, data=f.data,
                                    target=None,
                                    people=sorted({a.lower() for a in _ADDRESS.findall(text)}))
        except Exception:  # noqa: BLE001 - not kept: the file is still read for the answer
            return None
        return f"file:{row['file_id']}"





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


def _attached_kept(backend: Any, reader: Reader, f: "ask_attachments.Attached", guard: Any
                   ) -> "ask_attachments.Attached":
    """``f`` once kept as the reader's own file (``_keep_attached``), its bytes and whole
    text dropped. A file cut for the question says so first in its text, with the
    ``file:`` citation ``plx.read_document`` reads the rest of it by; an editable one
    also carries that citation as ``kept`` (``plx.edit_document`` edits it)."""
    from operating import ask_attachments
    kept = backend._keep_attached(reader, f, guard)
    text = f.text
    if kept and f.cut:
        total = len(f.whole) if f.whole else None
        text = ("[The question holds only the start of this file" + (
            f" (its whole text is {total:,} characters)" if total else "")
            + "; plx.read_document " + json.dumps({"citation": kept})
            + " reads all of it: offset pages on, find searches it.]\n" + text)
    elif f.cut and f.whole:
        # the rest of this file could not be kept to read on (a session that is not the
        # person's own, a file the assistant may not read whole): said, never silent
        text = (f"[The question holds only the start of this file ({len(f.whole):,} "
                "characters in all); the rest could not be kept to read on here, so it was "
                "NOT read: say so in the answer.]\n" + text)
    if f.ocr_pending:
        # a scan of more pages than one request reads: say how many were, and read on
        started = kept and f.kind == "pdf" and backend._ocr_rest(reader, kept)
        done = f.ocr_total - f.ocr_pending
        text = (f"[Scanned pages: {done} of {f.ocr_total} were read by OCR for this answer; "
                + (f"the other {f.ocr_pending} are still being read in the background: "
                   "ask again in a few minutes and plx.read_document will have them.]\n"
                   if started else f"the other {f.ocr_pending} were not read.]\n") + text)
    editable = ask_attachments.EDITABLE.get(f.media_type or "") is not None
    return dataclasses.replace(f, text=text, data=None, whole=None,
                               kept=kept if editable else None)
