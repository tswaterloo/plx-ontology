"""Fill a form from the records for one reader (``operating.doc_fill``): the Ask's "fill this
form for PO 1234" with the form attached, and the Autopilot page's form filler
(``POST /api/doc-fill``).

* **The record** is the page's own record, a record id, or the one record the words name
  by an exact identifier match (``suggest``); two, and nothing is filled (the answer says
  to name one). With none named, the form's own identifiers name it (``form_sources``).
* **The facts** are the record's and its declared neighbours' property values as this
  reader's own tools read them (``record_facts``: the read policy applies there), and
  what the form's own thread writes when it came in an email, the records its identifiers
  name exactly, and the reader's own earlier copies of the same form agree on
  (``form_sources``, ``operating.doc_gather.form_facts``), planned nearest source first
  (``doc_gather.plan_form``). With no record and nothing read, nothing is filled (the
  answer says to name the record). A value that names a person the people exclusion
  keeps out never enters the file, nor a payment card's number, security code or expiry
  (``_card_fact``): a card authorisation form gets those blanks left for the reader to
  write themselves, even when an earlier filled copy holds them. One rule for every form
  (no guess at which form is the reader's own payment form): the filled file is served
  raw (``/api/instruct/files/{id}``, the upload desk), its JSON preview masked, and both
  agree that the Workbench never writes a card number it read.
* **The result** is a new file kept for this reader only (``instruct_files.UploadDesk``:
  served to them, never to another reader), every blank listed with its outcome and source.
  When the form came from a SharePoint file the reader opened, the copy is prepared as an
  upload beside it (``<name> (filled).<ext>``, never a replace) that the reader previews
  and approves through the gated write path (dry run until the owner opens SharePoint).
  When it came in an email, a reply carrying the file is prepared as a card
  (``fill:<upload id>``, ``fill_reply_card``) the reader opens in the action dialog: the
  confirmed send path (``operating.card_actions``) previews the exact message with the file
  attached and sends it only on the reader's own confirm (their own mailbox, recipients
  checked, the file re-read and re-hashed, the people it names cleared, fail closed).

Nothing here sends, writes or uploads. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

from operating import doc_fill as df
from operating import card_actions as ca
from operating import doc_gather as dg
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
    "copies_invalid": "Form filling is paused: the index of earlier forms could not be read.",
    "not_open": "That is not something you can open.",
    "people_exclusion_unavailable": "This is paused: the list of people whose data stays "
                                    "out is not loaded.",
}
REPLY_MISSING = ("No reply can carry the filled copy: nothing was filled into a file. "
                 "Download what was filled and reply yourself.")
FILL_CARD = "fill:"
# the actions a filled copy's reply offers: the two that carry the file
FILL_KINDS = ("reply_in_thread", "send_email")
# "fill this in", "fill out the form", "complete this questionnaire"; never "fill me in"
_FILL = re.compile(r"\b(?:fill(?:\s+(?:in|out))?|complete)\b(?!\s+(?:me|us)\b)", re.I)
_RECORDISH = re.compile(r"(?:\b([A-Za-z]{1,12})[ #:.-]{0,3})?\b([A-Za-z]{0,6}[-#]?\d{3,}"
                        r"[A-Za-z0-9-]*)\b")
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_SYSTEM = {"bc": "BC", "graph-mail": "Email", "sharepoint": "File", "api": "Portal",
           "portal": "Portal", "sage": "Sage", "filemaker": "FileMaker", "mc": "MC",
           "teams": "Teams"}
MAX_LOOKUPS = 3


# a record property or form field that holds a card's number, security code or expiry
_CARD_KEY = re.compile(r"(?:^|[._\s:|/-])(?:cvv2?|cvc2?|csc|cvn|security[._\s:|/-]?code|"
                       r"card[._\s:|/-]?(?:number|no|num|code|exp\w*)|(?:credit|debit)[._\s:|/-]?"
                       r"card\w*|cc[._\s:|/-]?(?:number|no|num|exp\w*))(?:$|[._\s:|/-])", re.I)


def _card_fact(fact: "df.Fact") -> bool:
    """Whether ``fact`` carries a payment card's number (by the masking rule,
    ``operating.card_text``) or is a card field (number, security code, expiry): never
    written into a filled file."""
    from operating.card_text import has_card_number, mask_served
    value = fact.value if isinstance(fact.value, str) else ""
    return has_card_number(value) or mask_served(value) != value \
        or bool(_CARD_KEY.search(str(fact.key or "")))


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
    scheme = str(handle).split("://", 1)[0].lower()
    if scheme == "api":   # the Portal's mirror of another system (serving.system_of)
        from .serving import system_of as handle_system
        return handle_system(str(handle))
    return _SYSTEM.get(scheme, "File")


class DocFillMixin:
    """Mixed into the backends after ``InstructMixin`` (its people guard, upload desk,
    sources and reply addressing)."""

    def record_facts(self, reader: Reader, object_type: str, object_id: str
                     ) -> tuple[list[df.Fact], list[str], m.ObjectRef]:
        """The record's facts as this reader reads them (``doc_fill.facts_from``), notes on
        what was not read, and the record. KeyError: not a record they may read."""
        raise KeyError("object_not_found")

    def form_sources(self, reader: Reader, fields: list[df.FormField], *,
                     message: Optional[str], words: str, named: bool
                     ) -> tuple[list[df.Fact], list[str], Optional[m.ObjectRef]]:
        """Facts for the form from where its answers are, read as this reader
        (``doc_gather.form_facts``: its thread when ``message`` is the email it came in,
        the records its identifiers name unless the reader ``named`` one, their own
        earlier copies of the form), notes, and the record its identifiers named."""
        return [], [], None

    def _fill_record(self, reader: Reader, words: str, context: Mapping[str, Any]
                     ) -> Optional[tuple[str, str]]:
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
            return None
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
        try:
            fields = df.open_document(data, terms).fields()
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        named = self._fill_record(reader, record, context or {})
        facts: list[df.Fact] = []
        notes: list[str] = []
        ref: Optional[m.ObjectRef] = None
        if named is not None:
            try:
                facts, notes, ref = self.record_facts(reader, *named)
            except (KeyError, LookupError, PermissionError):
                raise _refuse("no_record") from None
            except df.FillRefused as refused:
                raise _refuse(refused.code) from None
        # the form's own thread (when it came in an email), the records its identifiers
        # name, the reader's earlier copies of it: read as the reader
        message = next((h for h in (reply_to, str((context or {}).get("handle") or ""))
                        if h and h.startswith("graph-mail://")), None)
        try:
            more, more_notes, found = self.form_sources(
                reader, fields, message=message, words=record, named=named is not None)
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        facts, notes, ref = facts + more, notes + more_notes, ref or found
        # a value naming a person kept out (or a secret-looking string) is not used, nor a
        # payment card's number, security code or expiry (``_card_fact``)
        facts = [f for f in facts if clean(f.value, guard, len(f.value) + 1) == f.value
                 and not _card_fact(f)]
        if named is None and not facts:
            raise _refuse("no_record")
        try:
            result = df.fill(data, facts, terms, notes, planner=dg.plan_form)
        except df.FillRefused as refused:
            raise _refuse(refused.code) from None
        if ref is None:
            ref = m.ObjectRef(object_id="", type="Request", label=(name or "the form")[:200],
                              open_url="")
        kind = "pdf" if result.kind.startswith("pdf") else result.kind
        new_name = df.filled_name(name, result.kind)
        target = None
        if source:
            try:
                opened = self.source(reader, source, 0)  # type: ignore[attr-defined]
            except (KeyError, LookupError, ValueError, PermissionError):
                raise _refuse("not_open") from None
            target = files.target_of(source, opened.fields or {}, opened.label)
        upload_id = download_url = card_id = None
        if result.data is not None:
            people = sorted({a.lower() for f in result.fields if f.status == "filled"
                             for a in _ADDRESS.findall(f.after)})
            row = self._uploads().prepare(  # type: ignore[attr-defined]
                reader.key, name=new_name, kind=kind, data=result.data, target=target,
                people=people,
                reply_to=reply_to if reply_to and reply_to.startswith("graph-mail://")
                else None)
            download_url = f"/api/instruct/files/{row['file_id']}"
            upload_id = row["upload_id"] if target is not None else None
            if row.get("reply_to"):
                card_id = FILL_CARD + row["upload_id"]
        reply = None
        if reply_to:
            reply = self._fill_reply(reader, reply_to, new_name, guard, card_id)

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

    def _fill_reply(self, reader: Reader, handle: str, attachment: str, guard: Any,
                    card_id: Optional[str]) -> m.FillReply:
        mat = self._source_material(reader, "E1", handle, guard)  # type: ignore[attr-defined]
        to = (self._reply_to(reader, mat) or {}).get("to")  # type: ignore[attr-defined]
        subject = mat.label if mat.label.lower().startswith("re:") else f"RE: {mat.label}"
        card = self._fill_card(reader, card_id, attachment, handle, mat) if card_id else None
        return m.FillReply(to=[to] if to else [], subject=subject[:200], attachment=attachment,
                           status="ready" if card else "not_available",
                           missing=[] if card else [REPLY_MISSING],
                           card_id=card["id"] if card else None,
                           actions=[o for o in ca.offers(card) if o["kind"] in FILL_KINDS]
                           if card else [])

    def _fill_card(self, reader: Reader, card_id: str, name: str, handle: str,
                   mat: Any) -> dict[str, Any]:
        """The card of a filled copy's reply: a reply in the thread when the email is in the
        reader's own mailbox, else a new email to its sender; either carries the file."""
        found = self._reply_to(reader, mat) or {}  # type: ignore[attr-defined]
        to = found.get("to") or None
        label = mat.label or "your request"
        subject = label if label.lower().startswith("re:") else f"RE: {label}"
        facts = [{"fact": "last_vendor_message", "value": label,
                  "source": {"handle": handle}}] if found.get("in_reply_to") else []
        return {"id": card_id, "headline": f"Send back {name}"[:300],
                "subject": {"label": label[:200]}, "action": {"text": ""},
                "draft": {"channel": "email",
                          "to": {"address": to, "source": handle} if to else None,
                          "subject": subject[:255],
                          "body": f"Please find {name} attached, filled in from our records.",
                          "facts": facts},
                "attachments": [card_id[len(FILL_CARD):]], "teams_draft": None,
                "playbook": True}

    def fill_reply_card(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        """The reply carrying a filled copy, rebuilt now from this reader's own kept file
        and the email it answers, read again as them (None: not theirs, or the email is
        no longer one they can open)."""
        upload = self._uploads().get(  # type: ignore[attr-defined]
            reader.key, card_id[len(FILL_CARD):])
        handle = (upload or {}).get("reply_to")
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if not isinstance(handle, str) or not handle.startswith("graph-mail://") \
                or guard is None:
            return None
        try:
            mat = self._source_material(reader, "E1", handle,  # type: ignore[attr-defined]
                                        guard)
        except UserFacing:
            return None
        return self._fill_card(reader, card_id, upload["name"], handle, mat)

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
        left = sum(v for k, v in n.items() if k not in ("filled", "kept", "suggested"))
        check = n.get("suggested", 0)
        source = filled.record.label if filled.record.object_id else \
            "its thread, the records it names and your earlier copies"
        partial = "Partial (the search ran out of time). " if dg.PARTIAL in filled.notes \
            else ""
        said = (partial + f"Filled {n['filled']} of {len(filled.fields)} blanks in "
                f"{filled.original} from {source}." + (f" {left} left for you to fill."
                                                         if left else "") +
                (f" {check} suggested from the form's earlier versions, not filled in: "
                 "check each before you enter it." if check else "") +
                " Check each one against its source before you save it.")
        def whose(c: m.FillCitation) -> str:
            return filled.record.label if c.record == filled.record.object_id \
                else system_of(c.source)
        facts = [m.Fact(text=f"{f.label}: {f.after}", sources=[m.Source(
            system=system_of(f.citation.source), handle=f.citation.source,
            label=f"{whose(f.citation)} {f.citation.field}"[:300],
            open_url=f.citation.open_url)])
            for f in filled.fields if f.status == "filled" and f.citation]
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=text, outcome="answered",
            direct_answer=said, facts=facts, hidden=m.Hidden(count=0),
            freshness=m.Freshness(),
            subjects=[filled.record] if filled.record.object_id else [],
            conversation_id=conversation,
            latency_ms=latency, drafts=[],
            instruction=m.InstructPart(status="drafted", text=text, fills=[filled],
                                       context=dict(context),
                                       read=[m.InstructRead(kind="attachment",
                                                            label=form[0][:200])]))


__all__ = ["DocFillMixin", "FILL_REFUSED", "fill_intent", "first_form"]
