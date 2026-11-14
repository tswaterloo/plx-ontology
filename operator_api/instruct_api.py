"""Tell it to do anything, anywhere: the Ask box's "do X". One box serves questions and
instructions; ``operating.instruct.route`` sends a line asking for something done here
(``instruct_ask``), a question to the cited answer path as before, and asks which when it
cannot tell. ``operating.instruct`` is the pure part; this is the reading and the desk.

* **As the reader.** The page's context (the box's ``source``, ``object_id`` or
  ``context``) is read through the reader's own reads: an email or file through ``source``
  (not theirs: refused, nothing else read, no model call), a record through
  ``object_page``, a meeting only when they may see its follow-up
  (``meeting_followup.visible``), an autopilot item only from their own area, and their own
  work log. Records the line or an attached file names are looked up with the reader's own
  search and kept only on an exact match. Every text is cleaned of excluded people
  (``feedback.clean``); with the people exclusion unavailable nothing is read.
* **Kept as their Ask.** The answer is the reader's Ask (the app's ``notes.ask`` keeps it
  in the work log, thread ``ask:<conversation>``), so each draft is an ``ask:<id>:<n>``
  card on the existing approvals desk, rebuilt from the log and re-checked at preview and
  confirm: the cited sources by ``ask_withheld``, the meetings used by
  ``instruction_withheld``. A reply to a message in the reader's own mailbox is offered in
  its thread (the desk reads its recipients from the message, refuses any other mailbox).
  A change no adapter makes is said plainly and drafted as steps a person does.
* **Exact writes** go to the write desk (``/api/writes``) prepared by the reader, who must
  hold the write grant; its preview shows each field now and after, another grant holder
  approves, and execution re-reads the record (an etag that moved is refused).
* **Documents** become files (``operating.instruct_files``) with an upload draft to the
  SharePoint folder of a file the reader opened; its preview re-opens that file as the
  reader, and an approval is recorded ``would_upload`` (no upload path in this build).
* **Recurring** lines come back as a workflow proposed in Draft mode.

Nothing here sends, writes or uploads. Evidence: ``isolated-fixture`` / injected model.
"""

from __future__ import annotations

import functools
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import quote

from operating import card_actions as ca
from operating import instruct as ins
from operating import instruct_files as files

from . import doc_fill_api as dfa
from . import doc_style_api as dsa
from . import instruct_assist as ia
from . import models as m
from .ask_drafts import CARD_PREFIX, draft_card
from .auth import Reader
from .errors import UserFacing

REFUSED = {
    "empty": ("Type what you want done.", 422),
    "too_long": ("Keep the instruction under 1000 characters.", 422),
    "people_exclusion_unavailable": ("This is paused: the list of people whose data stays "
                                     "out is not loaded.", 503),
    "model_unavailable": ("The assistant is not set up on this server.", 503),
    "not_open": ("That is not something you can open.", 404),
    "not_yours": ("That instruction is not one of yours.", 404),
    "no_draft": ("The assistant came back with nothing usable. Say it another way, or add "
                 "what it should use.", 502),
    "not_kept": ("The draft could not be kept, so it cannot go to the approvals desk.", 503),
    "context_incomplete": ("The page's context is incomplete.", 422),
}
MAX_LOOKUPS = 3
MAX_LOG = 8
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_RECORDISH = re.compile(r"\b[A-Za-z]{0,6}[-#]?\d{3,}[A-Za-z0-9-]*\b")
_SYSTEMS = ("BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC")
_DETAIL = re.compile(r"^\s*([A-Za-z][A-Za-z /-]{0,30}):\s*(.+?)\s*$")
_READ_KIND = {"email": "email", "autopilot item": "email", "record": "record",
              "meeting": "meeting", "attachment": "attachment"}
# a drafted change's System: the autopilot's write adapter for it
_ADAPTER_OF = {"bc": "bc_write", "business central": "bc_write", "mc": "mc_approval",
               "mission control": "mc_approval", "sage": "sage_write",
               "portal": "portal_write", "filemaker": "filemaker_write"}
ROUTE_QUESTION = "Should I draft this for your approval, or answer it from the records?"


def _refuse(code: str) -> UserFacing:
    text, status = REFUSED[code]
    return UserFacing("instruct_refused", text, status)


def _clean_source(source: m.Source, guard: Any) -> m.Source:
    """A record fact's source as the answer cites it: its handle kept, its words cleaned of
    excluded people."""
    from .feedback import clean
    return source.model_copy(update={
        "label": clean(source.label, guard, 300) or source.label,
        "quote": clean(source.quote, guard, 500) if source.quote else source.quote,
        "fields": {k: clean(str(v), guard, 500) or "" for k, v in source.fields.items()}
        if source.fields else source.fields})


def _sender_of(fields: Mapping[str, str]) -> Optional[str]:
    for key, value in fields.items():
        if key.rsplit(".", 1)[-1].lower() in ("from", "from_address", "sender",
                                               "sender_address"):
            found = _ADDRESS.search(str(value or ""))
            if found:
                return found.group(0).lower()
    return None


class InstructMixin(ia.InstructAssistMixin):
    """Needs ``source``, ``object_page``, ``suggest``, ``work_log`` and the write desk
    (``WriteIntentsMixin``); tests set ``instruct_call`` (the model) and may set
    ``instruct_uploads`` and ``instruct_history`` (``instruct_assist``)."""

    instruct_call: Any = None
    instruct_uploads: Any = None

    # ---- what the reader may read ------------------------------------------------------

    def _instruct_guard(self) -> Any:
        try:
            people = getattr(self, "people_guard", None)
            guard = people() if callable(people) else None
            if guard is None and getattr(self, "work_log", None) is not None:
                guard = self.work_log.guard()
        except Exception:  # noqa: BLE001 - undecidable: nothing read
            return None
        if guard is None or getattr(guard, "unavailable", "absent"):
            return None
        return guard

    def _meeting_store(self) -> Any:
        from operating import meeting_followup as mf
        state = getattr(self, "state_dir", None)
        if not state:
            return None
        try:
            return mf.FollowUps(state, getattr(self, "meeting_people", None))
        except Exception:  # noqa: BLE001 - no store: no meetings
            return None

    def _meeting_viewer(self, reader: Reader) -> Any:
        from .briefs import _viewer
        try:
            held = self.feedback_grant(reader, [])[0]  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - no markings decided: none held
            held = frozenset()
        return _viewer(reader, frozenset(held))

    def _visible_meetings(self, reader: Reader) -> list[dict[str, Any]]:
        from operating import meeting_followup as mf
        store = self._meeting_store()
        if store is None:
            return []
        viewer = self._meeting_viewer(reader)
        return [r for r in store.all() if mf.visible(r, viewer)]

    def instruction_withheld(self, reader: Reader, answer: Mapping[str, Any]
                             ) -> Optional[str]:
        """Why a kept instruction's drafts are not open to this reader now: a meeting it
        used that they may no longer see, or a record it read that they can no longer open
        (None: every one still theirs). Its cited sources are ``ask_withheld``'s."""
        part = (answer or {}).get("instruction")
        if not part:
            return None
        for subject in (answer or {}).get("subjects") or ():
            try:
                self.object_page(reader, subject["type"],  # type: ignore[attr-defined]
                                 subject["object_id"], quick=True)
            except (KeyError, LookupError, PermissionError, TypeError):
                return "withheld"
        used = part.get("meetings_used") or []
        if not used:
            return None
        seen = {str((r.get("meeting") or {}).get("meeting_id"))
                for r in self._visible_meetings(reader)}
        return "withheld" if any(mid not in seen for mid in used) else None

    def _source_material(self, reader: Reader, key: str, handle: str, guard: Any,
                         kind: str = "email") -> ins.Material:
        from .feedback import clean
        try:
            got = self.source(reader, handle, 0)  # type: ignore[attr-defined]
        except (KeyError, LookupError, ValueError, PermissionError):
            raise _refuse("not_open") from None
        fields = {k: clean(str(v), guard, 500) or "" for k, v in (got.fields or {}).items()}
        return ins.Material(key, kind, clean(got.title or got.label, guard, 300) or got.label,
                            clean(got.text or "", guard, ins.MAX_MATERIAL) or "", handle,
                            fields, system=got.system,
                            open_url=f"/source?handle={quote(handle, safe='')}")

    def _record_material(self, reader: Reader, key: str, object_type: str, object_id: str,
                         guard: Any) -> tuple[ins.Material, list[m.Fact]]:
        """A record's page as the reader reads it, and its facts with their sources (the
        answer cites them, so the desk re-checks each at preview and confirm)."""
        from .feedback import clean
        page = self.object_page(reader, object_type, object_id,  # type: ignore[attr-defined]
                                quick=True)
        lines = [clean(page.summary, guard, 500) or ""]
        facts: list[m.Fact] = []
        for fact in page.facts[:25]:
            said = clean(fact.text, guard, 400) or ""
            lines.append("- " + said)
            facts.append(m.Fact(text=said or page.label, sources=[
                _clean_source(s, guard) for s in fact.sources]))
        return (ins.Material(key, "record", clean(page.label, guard, 300) or page.label,
                             "\n".join(lines)[:ins.MAX_MATERIAL], None,
                             system="BC", open_url=f"/object/{quote(page.type, safe='')}/"
                                                   f"{quote(page.object_id, safe='')}"),
                facts)

    def _gather(self, reader: Reader, text: str, context: Mapping[str, Any], guard: Any,
                attached: list[Any], meetings_used: list[str]) -> dict[str, Any]:
        """The reader's materials for one instruction (module doc). Raises ``not_open``
        when the page's own context is not theirs."""
        from .feedback import clean
        mats: list[ins.Material] = []
        cited: list[m.Fact] = []       # records' facts: cited, and their write targets
        records: list[dict[str, str]] = []
        reply: Optional[dict[str, str]] = None
        kind = context.get("kind") or "page"
        n = {"E": 0, "R": 0}

        def key(prefix: str) -> str:
            n[prefix] += 1
            return f"{prefix}{n[prefix]}"

        if kind in ("email", "source"):
            if not context.get("handle"):
                raise _refuse("context_incomplete")
            mat = self._source_material(reader, key("E"), context["handle"], guard)
            mats.append(mat)
            reply = self._reply_to(reader, mat)
        elif kind == "record":
            if not (context.get("object_type") and context.get("object_id")):
                raise _refuse("context_incomplete")
            try:
                mat, got = self._record_material(reader, key("R"), context["object_type"],
                                                 context["object_id"], guard)
            except (KeyError, LookupError, PermissionError):
                raise _refuse("not_open") from None
            mats.append(mat)
            cited += got
            records.append({"object_id": context["object_id"],
                            "type": context["object_type"], "label": mat.label})
        elif kind == "meeting":
            mid = str(context.get("meeting_id") or "")
            found = next((r for r in self._visible_meetings(reader)
                          if str((r.get("meeting") or {}).get("meeting_id")) == mid), None)
            if found is None:
                raise _refuse("not_open")
            if mid not in meetings_used:
                meetings_used.append(mid)
        elif kind == "autopilot":
            item = self._own_autopilot_item(reader, str(context.get("item_id") or ""))
            if item is None:
                raise _refuse("not_open")
            mats.append(ins.Material(key("E"), "autopilot item",
                                     clean(item.get("title"), guard, 300) or "Item",
                                     clean(item.get("summary") or "", guard, 2000) or "",
                                     system="Email"))
            mail = next((c for c in item.get("citations") or ()
                         if isinstance(c, str) and c.startswith("graph-mail://")), None)
            if mail:
                try:
                    mat = self._source_material(reader, key("E"), mail, guard)
                    mats.append(mat)
                    reply = self._reply_to(reader, mat)
                except UserFacing:
                    pass   # the item's own words only
        # records the instruction or an attached file names, on an exact match only
        words = " ".join([text] + [a.text[:4000] for a in attached if a.text])
        tokens = list(dict.fromkeys(_RECORDISH.findall(words)))
        unread = self._look_up(reader, tokens[:MAX_LOOKUPS], mats, cited, records, key, guard)
        unread += ["+" + t for t in tokens[MAX_LOOKUPS:]]     # never looked up: said too
        for number, a in enumerate(attached, 1):
            if a.text:
                mats.append(ins.Material(f"A{number}", "attachment", a.name, a.text[:6000],
                                         system="File"))
        visible = self._visible_meetings(reader)
        for number, record in enumerate([r for r in visible if str(
                (r.get("meeting") or {}).get("meeting_id")) in meetings_used], 1):
            mats.append(ins.meeting_material(f"M{number}u", record))
        log = self._log_material(reader, records, mats, guard)
        if log is not None:
            mats.append(log)
        blob = " ".join([text] + [mt.label for mt in mats])
        candidates = [c for c in ins.rank_meetings(visible, blob)
                      if c.meeting_id not in meetings_used]
        return {"materials": mats, "facts": cited,
                "sources": [src for fact in cited for src in fact.sources],
                "records": records, "reply": reply, "unread": unread,
                "candidates": candidates}

    def _look_up(self, reader: Reader, tokens: list[str], mats: list[ins.Material],
                 cited: list[m.Fact], records: list[dict[str, str]],
                 key: Callable[[str], str], guard: Any) -> list[str]:
        """Each of ``tokens`` read as the record the reader's own search finds for it on an
        exact match (else not read), added to the materials, facts and records; the tokens
        no record the reader may read holds exactly (``unread_notes`` says them)."""
        unread: list[str] = []
        for token in tokens:
            try:
                hits = self.suggest(reader, token, 3).suggestions  # type: ignore[attr-defined]
                hit = next((h for h in hits if h.match == "exact"), None)
                if hit is None:
                    unread.append(token)
                    continue
                if any(r["object_id"] == hit.object_id for r in records):
                    continue
                mat, got = self._record_material(reader, key("R"), hit.type, hit.object_id,
                                                 guard)
            except Exception:  # noqa: BLE001 - not theirs, or the read failed: not read
                unread.append("?" + token)      # could not be read, never "no record"
                continue
            mats.append(mat)
            cited += got
            records.append({"object_id": hit.object_id, "type": hit.type,
                            "label": mat.label})
        return unread

    def _reply_to(self, reader: Reader, mat: ins.Material) -> Optional[dict[str, str]]:
        """A reply in the thread when the message is in the reader's own mailbox."""
        parts = ca.mail_parts(mat.handle)
        oid = str(getattr(reader.session, "subject_oid", "") or "").lower()
        sender = _sender_of(mat.fields)
        if parts is None or not oid or parts[0] != oid:
            return {"to": sender} if sender else None
        return {"in_reply_to": mat.handle or "", "to": sender or ""}

    def _own_autopilot_item(self, reader: Reader, item_id: str) -> Optional[dict[str, Any]]:
        try:
            area = self._area_read(reader)  # type: ignore[attr-defined]  # fresh, never kept
        except Exception:  # noqa: BLE001 - no area: no item
            return None
        for item in area.items:
            data = item.model_dump(mode="json")
            if data.get("item_id") == item_id and data.get("mine"):
                return data
        return None

    def _log_material(self, reader: Reader, records: list[dict[str, str]],
                      mats: list[ins.Material], guard: Any) -> Optional[ins.Material]:
        from .feedback import clean
        from .work_log import say
        store = getattr(self, "work_log", None)
        if store is None:
            return None
        seen: dict[int, Any] = {}
        try:
            for r in records[:3]:
                for e in store.events(reader=reader.key, record=r["object_id"],
                                      limit=MAX_LOG)[0]:
                    seen[e["seq"]] = e
            subject = next((mt.label for mt in mats if mt.kind == "email"), "")
            words = [w for w in re.findall(r"[A-Za-z0-9]{4,}", subject)][:3]
            if words:
                for e in store.events(reader=reader.key, q=" ".join(words), limit=MAX_LOG)[0]:
                    seen[e["seq"]] = e
        except Exception:  # noqa: BLE001 - no log read: none used
            return None
        if not seen:
            return None
        lines = [f"{e['at'][:10]} {clean(say(e), guard, 300)}"
                 for e in sorted(seen.values(), key=lambda e: -e["seq"])[:MAX_LOG]]
        return ins.Material("W1", "your work log", "Your earlier work on this",
                            "\n".join(lines), system="File")

    # ---- the model -----------------------------------------------------------------------

    def _instruct_model(self) -> Any:
        if self.instruct_call is not None:
            return self.instruct_call
        if getattr(self, "general_call", None) is not None:
            return self.general_call  # type: ignore[attr-defined]
        served = getattr(self, "_general_served", None)
        if served is None or not served():
            return None
        from operating import general_answer
        agent = getattr(self, "agent_ask", None)
        return functools.partial(general_answer.claude_call,
                                 binary=getattr(agent, "claude_bin", None) or "claude")

    # ---- the Ask path's "do" ---------------------------------------------------------------

    def instruct_ask(self, reader: Reader, request: m.AskRequest
                     ) -> Iterator[tuple[str, Any]]:
        """The Ask box's line when it asks for something done (``instruct.route``): the
        Ask's events, its answer carrying the drafts and the ``instruction`` part. The work
        log keeps it as the reader's Ask (the app's ``notes.ask``)."""
        started = time.monotonic()

        def ms() -> int:
            return int((time.monotonic() - started) * 1000)

        conversation = request.conversation_id or secrets.token_hex(8)
        yield "status", m.StatusEvent(step="understanding", message="Reading what to do",
                                      elapsed_ms=ms())
        try:
            text = ins.clean_text(request.question)
        except ins.InstructRefused as refused:
            raise _refuse(refused.code) from None
        context = self._context_of(request)
        self._assist_mail(reader, 0.0)   # the reader's own mail, read in the background
        if ins.route(text, mode=request.mode, continuing=request.instruction is not None) \
                == "unsure":
            # one question: do it, or answer it (the person's pick comes back as ``mode``)
            yield from self._said(m.AnswerPayload(
                answer_id=secrets.token_hex(12), question=text, outcome="answered",
                direct_answer=ROUTE_QUESTION, hidden=m.Hidden(count=0),
                freshness=m.Freshness(), conversation_id=conversation, latency_ms=ms(),
                instruction=m.InstructPart(status="route", text=text, question=ROUTE_QUESTION,
                                           context=context)))
            return
        guard = self._instruct_guard()
        if guard is None:
            raise _refuse("people_exclusion_unavailable")
        store = getattr(self, "work_log", None)
        if store is None:
            raise _refuse("not_kept")
        answers: list[str] = []
        meetings_used: list[str] = []
        files: list[dict[str, Any]] = []
        if request.instruction is not None:
            kept = store.ask_detail(reader.key, request.instruction.instruction_id)
            earlier = (kept or {}).get("instruction")
            if not isinstance(earlier, dict) or earlier.get("status") != "question":
                raise _refuse("not_yours")
            # the first turn's own words and page, never the request's
            text, context = earlier["text"], dict(earlier.get("context") or {})
            answers = list(earlier.get("answers") or [])
            offered = {c["meeting_id"] for c in earlier.get("meetings") or ()}
            meetings_used = list(earlier.get("meetings_used") or [])
            meetings_used += [u for u in request.instruction.use if u in offered
                              and u not in meetings_used]
            if request.instruction.answer and request.instruction.answer.strip():
                answers.append(ins.clean_text(request.instruction.answer))
            conversation = kept.get("conversation_id") or conversation
            files = [f for f in earlier.get("files") or () if isinstance(f, dict)]
        if request.instruction is None and request.attachments and dfa.fill_intent(text):
            form = dfa.first_form(request.attachments)
            if form is not None:
                # a form to fill: filled from the records, no model (``doc_fill_api``)
                yield "status", m.StatusEvent(step="reading", message="Filling the form",
                                              elapsed_ms=ms())
                yield from self._said(self.fill_answer(  # type: ignore[attr-defined]
                    reader, request, text, context, conversation, form, ms()))
                return
        if request.instruction is None and not request.attachments:
            try:
                styled = dsa.style_request(text)
            except Exception:  # noqa: BLE001 - kinds unreadable: answered below as asked
                styled = None
            if styled is not None:
                # a document in someone's own template, no model (``doc_style_api``)
                yield "status", m.StatusEvent(step="reading", message="Making the draft",
                                              elapsed_ms=ms())
                yield from self._said(self.style_answer(  # type: ignore[attr-defined]
                    reader, text, context, conversation, ms()))
                return
        call = self._instruct_model()
        if call is None:
            raise _refuse("model_unavailable")
        attached = self._instruct_attached(request, guard)
        # API-009: a continuation reads the first turn's files again, so they come again
        # with it (the app resends them); without them it says which to attach, never
        # drafting from the words alone
        missing = [f for f in files if f.get("digest") not in {a.digest for a in attached}]
        if missing:
            names = ", ".join(str(f.get("name") or "a file")[:80] for f in missing[:5])
            raise UserFacing("instruct_refused",
                             f"This instruction read {names}. Attach "
                             f"{'it' if len(missing) == 1 else 'them'} again to continue.",
                             409)
        known = {f.get("digest") for f in files}
        files += [a.meta for a in attached if a.digest not in known]
        yield "status", m.StatusEvent(step="reading", message="Reading what you have open",
                                      elapsed_ms=ms())
        got = self._gather(reader, text, context, guard, attached, meetings_used)
        # the assist's reading of the line (``instruct_assist``): short record numbers in
        # full, unless the message may go outside; a new message's recipients beside the
        # model call
        reply = got["reply"] or {}
        aim = self.assist_before(reader, text, bool(reply.get("in_reply_to")))
        if aim.expanded:
            numbered = [max((int(mt.key[1:]) for mt in got["materials"]
                             if mt.key[:1] == "R" and mt.key[1:].isdigit()), default=0)]

            def key(prefix: str) -> str:
                numbered[0] += 1
                return f"{prefix}{numbered[0]}"
            got["unread"] += self._look_up(reader, aim.expanded, got["materials"],
                                           got["facts"], got["records"], key, guard)
            got["sources"] = [src for fact in got["facts"] for src in fact.sources]
        # a record the line names that nothing the reader may read holds: said, never
        # dropped in silence (``instruct_assist.unread_notes``)
        aim.records = ia.unread_questions(text, got["unread"], aim.expanded)
        pending = None if reply.get("in_reply_to") or aim.recipients is not None \
            else self.assist_recipients(reader, text)
        targets, folders = self._targets(got["materials"], got["sources"])
        said_prompt = ins.prompt(text, got["materials"], got["candidates"],
                                 who=reader.display_name,
                                 page=str(context.get("page") or context.get("kind")),
                                 answers=answers, targets=targets, folders=folders)
        images = [{"media_type": a.media_type, "data": a.image} for a in attached
                  if a.image]
        yield "status", m.StatusEvent(step="answering", message="Drafting", elapsed_ms=ms())
        try:
            said = call(said_prompt, images=images) if images else call(said_prompt)
        except Exception:  # noqa: BLE001 - no reply
            said = None
        outcome = ins.read_reply(said, text, got["candidates"], targets, folders)
        if outcome is None:
            raise _refuse("no_draft")
        outcome = ins.ground_outcome(outcome, ins.given_text(text, got["materials"], answers))
        aim = ia.settle(pending, aim) if outcome.status != "question" else aim
        yield from self._said(self._answer(
            reader, guard, aim=aim, text=text, context=context, answers=answers,
            meetings_used=meetings_used, got=got, outcome=outcome, conversation=conversation,
            targets=targets, folders=folders, latency=ms(), attached=attached, files=files))

    @staticmethod
    def _said(answer: m.AnswerPayload) -> Iterator[tuple[str, Any]]:
        for word in re.findall(r"\S+\s*", answer.direct_answer):
            yield "token", m.TokenEvent(text=word)
        yield "answer", answer

    @staticmethod
    def _context_of(request: m.AskRequest) -> dict[str, Any]:
        """The page's context: the box's ``source`` (an email or file) or ``object_id`` (a
        record, its type read from the id's own prefix) first, else ``context``."""
        if request.source:
            return {"kind": "email", "handle": request.source}
        if request.object_id:
            kind, _, _ = request.object_id.partition(":")
            return {"kind": "record", "object_type": kind, "object_id": request.object_id}
        if request.context is not None:
            return request.context.model_dump()
        return {"kind": "page"}

    def _instruct_attached(self, request: m.AskRequest, guard: Any) -> list[Any]:
        """The request's files as read (``operating.ask_attachments``: its limits), their
        text cleaned of excluded people; nothing of them is kept."""
        if not request.attachments:
            return []
        import dataclasses
        from operating import ask_attachments
        from .feedback import clean
        batch = ask_attachments.decode(a.model_dump() for a in request.attachments)
        return [dataclasses.replace(f, text=clean(f.text, guard, len(f.text) + 1) or "")
                if f.text else f for f in batch.read]

    def _targets(self, mats: list[ins.Material], sources: list[Any]
                 ) -> tuple[list[ins.WriteTarget], list[ins.Folder]]:
        from integrations.live_source_guard import parse_handle
        from operating import write_intents as wi
        by_set = {op["entity_set"]: (name, op) for name, op in wi.OPERATIONS.items()
                  if op.get("method") == "PATCH"}
        targets: list[ins.WriteTarget] = []
        folders: list[ins.Folder] = []
        seen: set[str] = set()
        handles = [(s.handle, s.label) for s in sources] + \
            [(mt.handle, mt.label) for mt in mats if mt.handle]
        for handle, label in handles:
            if not handle or handle in seen:
                continue
            seen.add(handle)
            if handle.startswith("bc://"):
                try:
                    (_env, _co, entity_set, _id), _v, _x = parse_handle(handle, "bc", 4)
                except ValueError:
                    continue
                if entity_set in by_set and len(targets) < 6:
                    name, op = by_set[entity_set]
                    targets.append(ins.WriteTarget(f"T{len(targets) + 1}", label, handle,
                                                   name, tuple(op["fields"])))
            elif handle.startswith("sharepoint://") and len(folders) < 4:
                folders.append(ins.Folder(f"F{len(folders) + 1}",
                                          f"the folder of {label}", handle))
        return targets, folders

    # ---- the result ----------------------------------------------------------------------

    def _answer(self, reader: Reader, guard: Any, *, text: str, context: dict[str, Any],
                answers: list[str], meetings_used: list[str], got: dict[str, Any],
                outcome: ins.Outcome, conversation: str, targets: list[ins.WriteTarget],
                folders: list[ins.Folder], latency: int, attached: list[Any],
                files: Optional[list[dict[str, Any]]] = None,
                aim: Optional[ia.Aim] = None) -> m.AnswerPayload:
        from .feedback import clean
        aim = aim or ia.Aim()
        answer_id = secrets.token_hex(12)
        reply = got["reply"] or {}
        asking = outcome.status == "question"
        drafts: list[dict[str, Any]] = []
        for n, d in enumerate(() if asking else outcome.drafts):
            card_id = f"{CARD_PREFIX}{answer_id}:{n}"
            body = clean(d.body, guard, 8000) or ""
            missing = [f"Fill in: {', '.join(outcome.placeholders)}."] \
                if outcome.placeholders and "[" in body else []
            draft: dict[str, Any] = {"id": card_id, "kind": d.kind, "sent": False,
                                     "body": body, "details": [],
                                     "missing": missing + [clean(n, guard, 300) or ""
                                                           for n in aim.notes]}
            if d.kind == "email":
                draft["title"] = (clean(d.subject, guard, 200) or "Draft email")[:200]
                draft["email_subject"] = draft["title"]
                if reply.get("in_reply_to"):
                    draft["in_reply_to"] = reply["in_reply_to"]
                    draft["to"] = reply.get("to") or None
                    kinds = ("reply_in_thread", "send_email")
                else:
                    kinds = ("send_email",)
                    plain = (lambda t: clean(t, guard, 300) or "")
                    to, said_to = ia.address_notes(aim.recipients, plain)
                    # a proposed address the reader's own exclusion check would strip
                    # is never proposed
                    draft["to"] = to if to and clean(to, guard, 300) == to else None
                    draft["missing"][:0] = said_to
                    if draft["to"] and aim.recipients is not None:
                        draft["also_to"] = [a for a in aim.recipients.to[1:] if plain(a) == a]
                        draft["cc"] = [a for a in aim.recipients.cc if plain(a) == a]
                    draft["recipient_questions"] = ia.recipient_questions(aim.recipients,
                                                                          plain)
                draft["record_questions"] = [
                    dict(q, record=clean(q["record"], guard, 100) or "",
                         note=clean(q["note"], guard, 300) or "",
                         ask_line=clean(q["ask_line"], guard, 300) or "")
                    for q in aim.records]
            else:
                details = [{"label": g.group(1).strip(), "value": g.group(2)[:500]}
                           for g in map(_DETAIL.match, body.splitlines()) if g][:20]
                what = next((x["value"] for x in details if x["label"].lower() == "what"),
                            "")
                system = next((x["value"] for x in details
                               if x["label"].lower() == "system"), "")
                draft.update(title=(what or "Draft change")[:200], email_subject=None,
                             details=details)
                said_how, kinds = self._write_path(system, draft)
                draft["missing"].append(said_how)
                draft["missing"] += [clean(q["note"], guard, 300) or "" for q in aim.records]
            draft["actions"] = [o for o in ca.offers(draft_card(card_id, draft))
                                if o["kind"] in kinds]
            drafts.append(draft)
        writes = [] if asking else [self._prepare_write(reader, w, targets, text, answers)
                                    for w in outcome.writes]
        documents = [] if asking else [self._prepare_document(reader, doc, folders, guard)
                                       for doc in outcome.documents]
        facts = [m.Fact(text=mt.label, sources=[m.Source(
            system=mt.system, handle=mt.handle, label=mt.label[:300],
            fields=dict(list(mt.fields.items())[:20]) or None, open_url=mt.open_url)])
            for mt in got["materials"] if mt.handle and mt.system in _SYSTEMS]
        # every record read is cited by its own facts' sources, so the desk re-checks the
        # reader's access to each at preview and confirm (``ask_withheld``); a source with
        # no handle cannot be decided there and withholds the drafts (fail closed)
        facts += got["facts"]
        meetings = [m.InstructMeeting(meeting_id=c.meeting_id, title=c.title, day=c.day)
                    for c in got["candidates"]]
        proposed = [mt for c, mt in zip(got["candidates"], meetings) if c.key in outcome.use]
        said = clean(outcome.said, guard, 400) or ""
        question = clean(outcome.question, guard, 400) if asking else None
        read = [m.InstructRead(kind=_READ_KIND.get(mt.kind, "work_log"), label=mt.label,
                               open_url=mt.open_url or ("/work-log" if mt.key == "W1"
                                                        else None))
                for mt in got["materials"]]
        read += [m.InstructRead(kind="attachment", label=a.name) for a in attached
                 if a.image]
        return m.AnswerPayload(
            answer_id=answer_id, question=text, outcome="answered",
            direct_answer=question or said or "Drafted for your approval.",
            facts=facts, hidden=m.Hidden(count=0), freshness=m.Freshness(),
            subjects=[m.ObjectRef(object_id=r["object_id"], type=r["type"], label=r["label"],
                                  open_url=f"/object/{quote(r['type'], safe='')}/"
                                           f"{quote(r['object_id'], safe='')}")
                      for r in got["records"]],
            conversation_id=conversation, latency_ms=latency,
            drafts=[m.AskDraft.model_validate(d) for d in drafts],
            instruction=m.InstructPart(
                status=outcome.status, text=text, question=question,
                options=[c for o in outcome.options if (c := clean(o, guard, 140))]
                if asking else [],
                proposed=proposed,
                meetings=meetings, writes=writes, documents=documents,
                workflow=m.InstructWorkflow.model_validate(outcome.workflow)
                if outcome.workflow else None, read=read, context=context,
                answers=answers, meetings_used=meetings_used,
                files=[m.AskAttachmentNote(**f) for f in files or ()]))
    def _write_path(self, system: str, draft: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        """How a drafted change in ``system`` gets done, in words, and the desk's offers. A
        Teams message goes out from the desk's Teams action (a configured chat, your
        confirm); a system the autopilot has a write adapter for says what that adapter
        still lacks (``autopilot_write_adapters``, never a pretended success); any other
        system has no write path. Every one can be assigned or kept as a follow-up."""
        from operating import autopilot_write_adapters as wa
        local = ("create_follow_up", "assign")
        name = system.strip().lower()
        if name in ("teams", "microsoft teams", "teams chat"):
            return ("Send it from Teams here, to a chat set up for the Workbench, with your "
                    "confirm; or assign it.", ("teams_message", *local))
        kind = _ADAPTER_OF.get(name)
        adapter = wa.default_adapters({}).get(kind) if kind else None
        if adapter is not None:
            why = adapter.unavailable()
            if why is None:   # pragma: no cover - no transport is built in this build
                why = "it runs only from an approved autopilot item"
            needs = "; ".join(adapter.needs)
            return (f"The Workbench's {system} write path cannot run this yet ({why}"
                    + (f"; it needs {needs}" if needs else "") + "). Assign it to the "
                    "person who does it, or keep it as a follow-up.", local)
        return (f"The Workbench has no write path into {system or 'that system'}: assign "
                "it to the person who does it, or keep it as a follow-up.", local)

    def _prepare_write(self, reader: Reader, write: Mapping[str, str],
                       targets: list[ins.WriteTarget], text: str, answers: list[str]
                       ) -> m.InstructWrite:
        """An exact write on the write desk, prepared by the reader (module doc)."""
        from integrations.live_source_guard import parse_handle
        from operating import write_intents as wi
        target = next(t for t in targets if t.key == write["target"])
        label = f"{target.label}: {write['field']} to {write['value']}"
        if not ins.stated(write["value"], " ".join([text, *answers])):
            return m.InstructWrite(label=label, status="refused", problems=[
                "An exact change takes its new value from your own words: say the value."])
        (env, company, entity_set, record_id), _v, _x = parse_handle(target.handle, "bc", 4)
        op = wi.OPERATIONS[target.operation]
        person = self._person(reader)  # type: ignore[attr-defined]
        ctx = self._write_ctx()  # type: ignore[attr-defined]
        if not ctx.granted().holds(person.ids(), "bc", op["entity_set"]):
            return m.InstructWrite(label=label, status="refused", problems=[
                f"You hold no write grant for BC {entity_set}."])
        now = ctx.current(env, company, entity_set, record_id)
        if not now or not now.get("etag"):
            return m.InstructWrite(label=label, status="refused", problems=[
                "The record could not be read from BC now, so the change cannot be "
                "checked."])
        try:
            found = self._wdesk().prepare({  # type: ignore[attr-defined]
                "system": "bc", "operation": target.operation, "environment": env,
                "company_id": company, "record_id": record_id,
                "payload": {write["field"]: write["value"]},
                "precondition": {"etag": now["etag"]},
                # the person's own words: a retried instruction prepares the same intent
                "reason": text, "citations": [target.handle]}, person)
            intent_id = found["intent"]["intent_id"]
            preview = self.write_preview(reader, intent_id)  # type: ignore[attr-defined]
        except wi.WriteRefused as refused:
            return m.InstructWrite(label=label, status="refused", problems=refused.problems)
        except UserFacing as refused:
            return m.InstructWrite(label=label, status="refused", problems=[refused.message])
        return m.InstructWrite(intent_id=intent_id, label=label, status="prepared",
                               changes=preview.changes, problems=preview.problems)

    def _uploads(self) -> files.UploadDesk:
        if self.instruct_uploads is None:
            self.instruct_uploads = files.UploadDesk.for_state_dir(
                getattr(self, "state_dir", None))
        return self.instruct_uploads

    def _prepare_document(self, reader: Reader, doc: Mapping[str, str],
                          folders: list[ins.Folder], guard: Any) -> m.InstructDocument:
        from .feedback import clean
        kind = doc["kind"]
        raw = str(doc.get("content") or "")
        # the file's words as drafts' are: no excluded person's address or object id and
        # no secret-looking string in the bytes anyone downloads or uploads (ACT-008)
        content = clean(raw, guard, max(len(raw), 1)) or ""
        if not content:
            raise _refuse("no_draft")
        try:
            data = files.make(kind, content)
        except files.FileRefused as refused:
            if refused.code != "pdf_unavailable":
                raise _refuse("no_draft") from None
            kind, data = "docx", files.make("docx", content)
        folder = next((f for f in folders if f.key == doc.get("folder")), None)
        target = None
        if folder is not None:
            try:
                opened = self.source(reader, folder.anchor, 0)  # type: ignore[attr-defined]
                target = files.target_of(folder.anchor, opened.fields or {}, opened.label)
            except (KeyError, LookupError, ValueError, PermissionError):
                target = None
        name = files.file_name(clean(doc.get("name"), guard, 300), kind)
        # the people the file names (an excluded one is already withheld from it, so
        # nothing of them is kept here): cleared by the people exclusion again before an
        # upload and before a download, for a person excluded since the draft
        people = sorted({a.lower() for a in _ADDRESS.findall(content)})
        row = self._uploads().prepare(reader.key, name=name, kind=kind, data=data,
                                      target=target, people=people)
        return m.InstructDocument(name=name, kind=kind, size=row["size"],
                                  digest=row["digest"], upload_id=row["upload_id"],
                                  download_url=f"/api/instruct/files/{row['file_id']}",
                                  folder=target.folder if target else None)

    # ---- uploads -------------------------------------------------------------------------

    def _writes(self, reader: Reader) -> Any:
        """Whether the reader holds a write or owner role on an upload's target folder now
        (``files.folder_writable`` over the SharePoint grants recorded for the folder and
        its anchor file); None when not known. ``folder_acls`` (drive, item id) -> the
        recorded permission list with its ``read_at``, or None; else the SharePoint capture
        under ``sharepoint_capture_dir``; neither: not known. A list read more than
        ``files.ACL_MAX_AGE`` ago is not known either."""
        acls = getattr(self, "folder_acls", None) or self._captured_acls()
        identities = frozenset(getattr(reader.session, "identities", ()) or ()) \
            | {i for i in (getattr(reader.session, "subject_oid", None),) if i}

        def writes(target: Mapping[str, Any]) -> Optional[bool]:
            from integrations.live_source_guard import parse_handle
            if acls is None:
                return None
            try:
                (drive, item), _v, _x = parse_handle(
                    str(target.get("anchor") or "").split("#", 1)[0], "sharepoint", 2)
                if drive != target.get("drive"):
                    return None
                parent = str(target.get("parent") or "")
                return files.folder_writable(acls(drive, parent) if parent else None,
                                             acls(drive, item), identities)
            except Exception:  # noqa: BLE001 - permissions that cannot be read: not known
                return None
        return writes

    def _captured_acls(self) -> Optional[Callable[[str, str], Any]]:
        """The SharePoint capture's recorded permission list for a drive item (its
        current version), read-only; None without a capture on this server."""
        folder = getattr(self, "sharepoint_capture_dir", None)
        if not folder:
            return None
        from integrations import capture_sharepoint as cs
        if not cs.store_exists(Path(folder)):
            return None

        def acl(drive: str, item: str) -> Any:
            store = cs.SharePointCaptureStore.open_read_only(Path(folder))
            try:
                row = store.current(drive, item)
            finally:
                store.close()
            if not row or not row.get("source_acl"):
                return None
            try:
                found = json.loads(row["source_acl"]) if isinstance(row["source_acl"], str) \
                    else dict(row["source_acl"])
            except (TypeError, ValueError):
                return None
            if not isinstance(found, dict):
                return None
            # when the capture last read this list (it is stored again each time it is
            # seen): an old one does not decide a write (ACT-009)
            return {**found, "read_at": row.get("last_seen_at") or row.get("recorded_at")}
        return acl

    def _acknowledge(self, reader: Reader, upload: Mapping[str, Any]) -> tuple[str, ...]:
        """The not-routine reasons the write path's rules give this upload now: the ones
        the preview shows and the reader's confirm accepts (none known: none accepted, and
        the write path stops it as not routine)."""
        found = self._upload_item(reader, upload)
        if found is None:
            return ()
        from operating import autopilot_write as aw
        from operating import workflows as wf
        item, executor, _me = found
        try:
            rules = aw.load_rules(executor.rules_path)
            action = aw.actions_of(item)[0]
            return tuple(aw.routine(rules, item, action, wf.of_item("instruction", {}),
                                    executor._known_domains()))
        except Exception:  # noqa: BLE001 - rules unreadable: nothing acknowledged
            return ()

    def _opens(self, reader: Reader) -> Any:
        def opens(handle: str) -> bool:
            try:
                self.source(reader, handle, 0)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - refused or failed: not open
                return False
            return True
        return opens

    def _upload_item(self, reader: Reader, upload: Mapping[str, Any]
                     ) -> Optional[tuple[dict[str, Any], Any, Any]]:
        """The approved upload as a write-path item drafted from the reader's instruction
        (``autopilot_write``: SharePoint ``file_upload``, never a replace), with its
        executor and the reader's ids; None without a write-path state on this server."""
        import base64
        from operating import autopilot_write as aw
        state = self._apa_state() if hasattr(self, "_apa_state") else None  # type: ignore[attr-defined]
        target = upload.get("target") or {}
        data = self._uploads().file(reader.key, upload["file_id"])
        if state is None or data is None:
            return None
        me = tuple(sorted(self._apa_me(reader)))  # type: ignore[attr-defined]
        owner = next((i for i in me if "@" in i), me[0])
        item = {"item_id": upload["upload_id"], "autopilot": "instruction", "owner": owner,
                "title": "Upload " + upload["name"], "citations": [target.get("anchor", "")],
                "body": {"label": "Upload a drafted document", "happened": {},
                         "prepared": {"write": {
                             "system": "sharepoint", "operation": "file_upload",
                             "target": {"drive_id": target.get("drive", ""),
                                        "parent_id": target.get("parent", "")},
                             "payload": {"name": upload["name"],
                                         "content_type": files.MEDIA.get(
                                             upload["kind"], "application/octet-stream"),
                                         "content_b64": base64.b64encode(data).decode()},
                             # the addresses the document names (its text travels only as
                             # bytes): the people exclusion clears them before an upload
                             "people": list(upload.get("people") or ())}}}}
        return item, self._apa_executor(state), me  # type: ignore[attr-defined]

    def _upload_missing(self, reader: Reader, upload: Mapping[str, Any]) -> Optional[list[str]]:
        """What a live upload lacks now (empty: approving it uploads it); None: no write
        path on this server (the desk's own wording)."""
        found = self._upload_item(reader, upload)
        if found is None:
            return None
        item, executor, _me = found
        missing = []
        if not (upload.get("target") or {}).get("parent"):
            missing.append("The folder's SharePoint id is not known: open a file in that "
                           "folder again.")
        if "sharepoint_write" not in executor.live_kinds(item):
            missing.append("SharePoint is not opened to approved writes (the owner's "
                           "PLX_APPROVED_WRITES lacks sharepoint_write).")
        adapter = executor.adapters.get("sharepoint_write")
        why = adapter.unavailable() if adapter is not None else "no SharePoint write path"
        if why:
            missing.append(str(why))
        return missing

    def upload_preview(self, reader: Reader, upload_id: str) -> m.UploadPreview:
        upload = self._uploads().get(reader.key, upload_id)
        if upload is None:
            raise UserFacing("not_found", "That upload is not one of yours.", 404)
        if upload["status"] == "prepared":
            missing = self._upload_missing(reader, upload)
        elif upload.get("ran"):
            missing = list(upload.get("reasons") or ())    # what the write path said
        else:
            # approved, the write path not run: a live one is being made; else recorded
            missing = [] if upload["status"] == "unknown" else None
        shown = files.UploadDesk.preview(
            upload, self._opens(reader), missing, writes=self._writes(reader),
            acknowledge=self._acknowledge(reader, upload)
            if upload["status"] == "prepared" else ())
        return m.UploadPreview(upload_id=upload_id, name=upload["name"], kind=upload["kind"],
                               size=upload["size"], digest=upload["digest"],
                               folder=(upload.get("target") or {}).get("folder"),
                               status=upload["status"],
                               acknowledge=shown["acknowledge_words"],
                               **{k: shown[k] for k in (
                                   "before", "after", "problems", "missing",
                                   "preview_hash")})

    def upload_approve(self, reader: Reader, upload_id: str, body: m.UploadApprove
                       ) -> m.UploadPreview:
        """Your approval of the upload as previewed, re-checked now; recorded as would
        upload (nothing reaches SharePoint in this build)."""
        upload = self._uploads().get(reader.key, upload_id)
        if upload is None:
            raise UserFacing("not_found", "That upload is not one of yours.", 404)
        if upload["status"] != "prepared":
            return self.upload_preview(reader, upload_id)      # approved once, run once
        # re-checked now as at preview: the reader opens the folder, holds write access
        # to it, and the hash covers what a live upload lacks and what the confirm accepts
        shown = files.UploadDesk.preview(upload, self._opens(reader),
                                         self._upload_missing(reader, upload),
                                         writes=self._writes(reader),
                                         acknowledge=self._acknowledge(reader, upload))
        try:
            mine = self._uploads().approve(reader.key, upload, shown, confirm=body.confirm,
                                           preview_hash=body.preview_hash)
        except files.FileRefused as refused:
            words = {"not_confirmed": "Nothing is done without your confirm.",
                     "problems": " ".join(shown["problems"]),
                     "preview_changed": "The upload changed since you saw it: preview it "
                                        "again."}[refused.code]
            raise UserFacing("action_refused", words, 409) from None
        # only the request that recorded the approval runs it (a double click's second
        # request reads the state the first one leaves)
        found = self._upload_item(reader, upload) if mine else None
        if found is not None:
            from datetime import datetime, timezone
            from operating import autopilot_write as aw
            item, executor, me = found
            # the reader's confirm of a preview that said it uploads now is their
            # confirm that it goes live; one that said it does not stays a dry run
            results = executor.run(item, datetime.now(timezone.utc), aw.Approval(
                by=me, origin="instruction", confirmed_live=bool(shown.get("live")),
                # the not-routine reasons the confirmed preview showed (its hash covers them)
                acknowledged=tuple(shown["acknowledge"])))
            last = results[-1] if results else {"status": "refused", "reasons": ["no_action"]}
            self._uploads().record_outcome(reader.key, upload_id, str(last.get("status")),
                                           list(last.get("reasons") or ()))
        return self.upload_preview(reader, upload_id)

    def instruct_file(self, reader: Reader, file_id: str) -> tuple[bytes, str]:
        data = self._uploads().file(reader.key, file_id)
        if data is None:
            raise UserFacing("not_found", "That file is not one of yours.", 404)
        rows = [r for r in self._uploads()._rows() if r.get("file_id") == file_id
                and r["reader"] == reader.key]
        # a person the document named, excluded since it was drafted (or no list to
        # decide with): the file is not served
        from .feedback import _excluded_address
        guard = self._instruct_guard()
        named = next((r["people"] for r in rows if r.get("people")), ())
        if named and (guard is None or any(_excluded_address(guard, str(a)) for a in named)):
            raise UserFacing("not_found", "That file can't be opened now.", 404)
        return data, rows[-1]["kind"] if rows else "docx"


__all__ = ["InstructMixin", "REFUSED"]
