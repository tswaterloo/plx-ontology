"""Fill a form from the records for one reader (``operating.doc_fill``): the Ask's "fill this
form for PO 1234" with the form attached, and the Autopilot page's form filler
(``POST /api/doc-fill``).

* **The record** is the page's own record, a record id, or the one record the words name
  by an exact identifier match (``suggest``); none, or two, and nothing is filled (the
  answer says to name it).
* **The facts** are the record's and its declared neighbours' property values as this
  reader's own tools read them (``record_facts``: the read policy applies there). A value
  that names a person the people exclusion keeps out never enters the file.
* **The result** is a new file kept for this reader only (``instruct_files.UploadDesk``:
  served to them, never to another reader), every blank listed with its outcome and source.
  When the form came from a SharePoint file the reader opened, the copy is prepared as an
  upload beside it (``<name> (filled).<ext>``, never a replace) that the reader previews
  and approves through the gated write path (dry run until the owner opens SharePoint).
  When it came in an email, a reply carrying the file is prepared but never sent: the send
  path takes no file.

Nothing here sends, writes or uploads. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

from operating import doc_fill as df
from operating import instruct_files as files

from . import models as m
from .auth import Reader
from .errors import UserFacing

FILL_REFUSED = {
    "empty": "That file is empty.",
    "too_large": "That file is too large to fill (8 MB at most).",
    "not_a_form": "That file is not a Word, Excel or PDF form that can be filled here.",
    "encrypted": "That PDF is locked with a password.",
    "no_fields": "No blanks to fill were found in that file.",
    "pdf_unavailable": "PDF forms can't be filled on this server.",
    "pdf_overlay_unavailable": "PDF forms can't be filled on this server.",
    "terms_invalid": "Form filling is paused: its list of labels could not be read.",
    "no_record": "Say which record the form is for, for example \"for PO 1234\".",
    "two_records": "Those words match more than one record. Say which one the form is for.",
    "records_unreadable": "The record could not be read in full, so nothing was filled.",
    "not_open": "That is not something you can open.",
    "people_exclusion_unavailable": "This is paused: the list of people whose data stays "
                                    "out is not loaded.",
}
REPLY_MISSING = ("A reply can't carry a file yet: the send path takes text only. Download "
                 "the filled copy and attach it to your reply.")
# "fill this in", "fill out the form", "complete this questionnaire"; never "fill me in"
_FILL = re.compile(r"\b(?:fill(?:\s+(?:in|out))?|complete)\b(?!\s+(?:me|us)\b)", re.I)
_RECORDISH = re.compile(r"(?:\b([A-Za-z]{1,12})[ #:.-]{0,3})?\b([A-Za-z]{0,6}[-#]?\d{3,}"
                        r"[A-Za-z0-9-]*)\b")
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_SYSTEM = {"bc": "BC", "graph-mail": "Email", "sharepoint": "File", "api": "Portal",
           "portal": "Portal", "sage": "Sage", "filemaker": "FileMaker", "mc": "MC",
           "teams": "Teams"}
MAX_LOOKUPS = 3


def _refuse(code: str, status: int = 422) -> UserFacing:
    return UserFacing("fill_refused", FILL_REFUSED[code], 404 if code == "not_open" else (
        503 if code in ("people_exclusion_unavailable", "terms_invalid") else status))


def fill_intent(text: str) -> bool:
    """Whether an instruction asks for a form to be filled."""
    return bool(_FILL.search(str(text or "")))


def first_form(attachments: Iterable[Any]) -> Optional[tuple[str, bytes]]:
    """The first attachment that is a Word, Excel or PDF file by its bytes."""
    for a in attachments:
        try:
            data = base64.b64decode(a.data, validate=True)
            df.detect(data)
        except (binascii.Error, ValueError, df.FillRefused):
            continue
        return a.name, data
    return None


def system_of(handle: str) -> str:
    return _SYSTEM.get(str(handle).split("://", 1)[0].lower(), "File")


class DocFillMixin:
    """Mixed into the backends after ``InstructMixin`` (its people guard, upload desk,
    sources and reply addressing)."""

    def record_facts(self, reader: Reader, object_type: str, object_id: str
                     ) -> tuple[list[df.Fact], list[str], m.ObjectRef]:
        """The record's facts as this reader reads them (``doc_fill.facts_from``), notes on
        what was not read, and the record. KeyError: not a record they may read."""
        raise KeyError("object_not_found")

    def _fill_record(self, reader: Reader, words: str, context: Mapping[str, Any]
                     ) -> tuple[str, str]:
        if context.get("kind") == "record" and context.get("object_id"):
            return str(context.get("object_type") or ""), str(context["object_id"])
        words = str(words or "").strip()
        head, sep, _rest = words.partition(":")
        if sep and re.fullmatch(r"[A-Z][A-Za-z]{2,40}", head) and " " not in words:
            return head, words                                       # a record id
        found: dict[str, str] = {}
        for match in list(_RECORDISH.finditer(words))[:MAX_LOOKUPS]:
            ident = match.group(2)
            tries = [f"{match.group(1)} {ident}", ident] if match.group(1) else [ident]
            for q in tries:
                try:
                    hits = self.suggest(reader, q, 3).suggestions  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - a failed lookup names nothing
                    hits = []
                exact = {h.object_id: h.type for h in hits if h.match == "exact"}
                if exact:
                    if len(exact) > 1:
                        raise _refuse("two_records")
                    found.update(exact)
                    break
        if not found:
            raise _refuse("no_record")
        if len(found) > 1:
            raise _refuse("two_records")
        object_id, object_type = next(iter(found.items()))
        return object_type, object_id

    def doc_fill(self, reader: Reader, *, name: str, data: bytes, record: str,
                 context: Optional[Mapping[str, Any]] = None, source: Optional[str] = None,
                 reply_to: Optional[str] = None) -> m.InstructFill:
        """The form filled for ``reader`` (module doc). UserFacing when it cannot be."""
        from .feedback import clean
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            raise _refuse("people_exclusion_unavailable")
        try:
            terms = df.load_terms()
            df.detect(data)
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        object_type, object_id = self._fill_record(reader, record, context or {})
        try:
            facts, notes, ref = self.record_facts(reader, object_type, object_id)
        except (KeyError, LookupError, PermissionError):
            raise _refuse("no_record") from None
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        # a value naming a person kept out (or a secret-looking string) is not used
        facts = [f for f in facts if clean(f.value, guard, len(f.value) + 1) == f.value]
        try:
            result = df.fill(data, facts, terms, notes)
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        kind = "pdf" if result.kind.startswith("pdf") else result.kind
        new_name = df.filled_name(name, result.kind)
        target = None
        if source:
            try:
                opened = self.source(reader, source, 0)  # type: ignore[attr-defined]
            except (KeyError, LookupError, ValueError, PermissionError):
                raise _refuse("not_open") from None
            target = files.target_of(source, opened.fields or {}, opened.label)
        upload_id = download_url = None
        if result.data is not None:
            people = sorted({a.lower() for f in result.fields if f.status == "filled"
                             for a in _ADDRESS.findall(f.after)})
            row = self._uploads().prepare(  # type: ignore[attr-defined]
                reader.key, name=new_name, kind=kind, data=result.data, target=target,
                people=people)
            download_url = f"/api/instruct/files/{row['file_id']}"
            upload_id = row["upload_id"] if target is not None else None
        reply = None
        if reply_to:
            reply = self._fill_reply(reader, reply_to, new_name, guard)

        def opens(handle: str) -> str:
            return f"/source?handle={quote(handle, safe='')}"
        shown = [m.FillField(
            label=clean(f.label, guard, 200) or "", before=clean(f.before, guard, 400) or "",
            after=f.after, status=f.status, reason=f.reason,
            citation=m.FillCitation(record=f.citation.record, field=f.citation.field,
                                    source=f.citation.source,
                                    open_url=opens(f.citation.source))
            if f.citation else None) for f in result.fields]
        return m.InstructFill(
            name=new_name, original=files.file_name(name, kind) if name else new_name,
            kind=kind, record=ref, counts=result.counts(), fields=shown,
            notes=list(result.notes)[:10], digest_before=result.digest_before,
            digest_after=result.digest_after, size=len(result.data or b""),
            download_url=download_url,
            upload_id=upload_id, folder=target.folder if target else None, reply=reply)

    def _fill_reply(self, reader: Reader, handle: str, attachment: str, guard: Any
                    ) -> m.FillReply:
        mat = self._source_material(reader, "E1", handle, guard)  # type: ignore[attr-defined]
        to = (self._reply_to(reader, mat) or {}).get("to")  # type: ignore[attr-defined]
        subject = mat.label if mat.label.lower().startswith("re:") else f"RE: {mat.label}"
        return m.FillReply(to=[to] if to else [], subject=subject[:200], attachment=attachment,
                           status="not_available", missing=[REPLY_MISSING])

    def doc_fill_request(self, reader: Reader, request: m.DocFillRequest) -> m.InstructFill:
        """``POST /api/doc-fill``."""
        try:
            data = base64.b64decode(request.data, validate=True)
        except (binascii.Error, ValueError):
            raise _refuse("not_a_form") from None
        source = request.source if request.source and request.source.startswith(
            "sharepoint://") else None
        return self.doc_fill(reader, name=request.name, data=data, record=request.record,
                             source=source, reply_to=request.reply_to)

    def fill_answer(self, reader: Reader, request: m.AskRequest, text: str,
                    context: Mapping[str, Any], conversation: str, form: tuple[str, bytes],
                    latency: int) -> m.AnswerPayload:
        """The Ask's answer to "fill this form for ...": the filled form to review."""
        import secrets
        handle = str(context.get("handle") or "")
        filled = self.doc_fill(
            reader, name=form[0], data=form[1], record=text, context=context,
            source=handle if handle.startswith("sharepoint://") else None,
            reply_to=handle if handle.startswith("graph-mail://") else None)
        n = filled.counts
        left = sum(v for k, v in n.items() if k not in ("filled", "kept"))
        said = (f"Filled {n['filled']} of {len(filled.fields)} blanks in {filled.original} "
                f"from {filled.record.label}." + (f" {left} left for you to fill." if left
                                                  else "") +
                " Check each one against its source before you save it.")
        facts = [m.Fact(text=f"{f.label}: {f.after}", sources=[m.Source(
            system=system_of(f.citation.source), handle=f.citation.source,
            label=f"{filled.record.label} {f.citation.field}"[:300],
            open_url=f.citation.open_url)])
            for f in filled.fields if f.status == "filled" and f.citation]
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=text, outcome="answered",
            direct_answer=said, facts=facts, hidden=m.Hidden(count=0),
            freshness=m.Freshness(), subjects=[filled.record], conversation_id=conversation,
            latency_ms=latency, drafts=[],
            instruction=m.InstructPart(status="drafted", text=text, fills=[filled],
                                       context=dict(context),
                                       read=[m.InstructRead(kind="attachment",
                                                            label=form[0][:200])]))


__all__ = ["DocFillMixin", "FILL_REFUSED", "fill_intent", "first_form"]
