"""Make a document in a person's own template: the Ask's "make a quote for Acme like Pat
does" (``operating.doc_style``).

* **The template** is one learned from the documents that person sent (the learned store,
  ``doc_style_mine``), of the kind the words name (``doc_style_kinds.json``), the one they
  use most. The person is named by a word of their address ("like Pat does", "in Pat's
  template"); none named: the reader's own. A name that fits two people, or none with a
  template of that kind, makes nothing and says so.
* **Who may use it**: the reader opens every document the template was learned from
  (``source``, the read policy every citation passes); one they may not, and the template
  is not theirs to use (it reads as none).
* **The facts** are gathered for the words after "for" across what the reader may read
  (``operating.doc_gather``: records, mail, attachments and files, prices quoted with
  their tiers), each cited. A value naming a person the people exclusion keeps out never
  enters the document.
* **The result** is a new file kept for this reader only (``instruct_files.UploadDesk``),
  every changing part listed with where its value came from or why it was left blank.

Nothing here sends, writes or uploads. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
import re
import secrets
from typing import Any, Mapping, Optional
from urllib.parse import quote

from operating import doc_fill as df
from operating import doc_gather as dg
from operating import doc_style as ds

from . import models as m
from .auth import Reader
from .doc_fill_api import system_of
from .errors import UserFacing

STYLE_REFUSED = {
    "no_templates": "No templates have been learned yet.",
    "templates_invalid": "Templates are paused: the learned list could not be read.",
    "kinds_invalid": "Templates are paused: the list of document kinds could not be read.",
    "no_kind": "Say what to make, for example \"a quote\" or \"a price list\".",
    "no_author": "No one by that name has a template of that kind you can use.",
    "two_authors": "That name fits more than one person. Use their full name or address.",
    "no_subject": "Say who or what it is for, for example \"for Acme\".",
    "exemplar_changed": "That template's last document changed since it was learned, so "
                        "nothing was made. It is learned again overnight.",
    "exemplar_missing": "That template's last document is no longer kept, so nothing was "
                        "made.",
    "unreadable": "That template's last document could not be read, so nothing was made.",
    "pdf_unavailable": "Templates from PDFs can't be used on this server.",
    "people_exclusion_unavailable": "This is paused: the list of people whose data stays "
                                    "out is not loaded.",
}
# "make a quote for X like Pat does", "draft a price list for X in Pat's template"
_MAKE = re.compile(r"\b(?:make|draft|write|prepare|create|do|put together)\b", re.I)
_LIKE = re.compile(r"\b(?:like|as)\s+([A-Za-z][A-Za-z.'-]{1,40})\s+(?:does|would|did|do)\b"
                   r"|\b(?:in|using|with)\s+([A-Za-z][A-Za-z.'-]{1,40})['’]s\s+"
                   r"(?:template|style|format|way|layout)\b", re.I)
_FOR = re.compile(r"\bfor\s+(.+?)(?=\s+(?:like|as|in|using|with)\s+[A-Za-z][A-Za-z.'-]*"
                  r"(?:\s+(?:does|would|did|do)\b|['’]s\s)|[.?!]?\s*$)", re.I)


def _refuse(code: str) -> UserFacing:
    status = 503 if code in ("templates_invalid", "kinds_invalid",
                             "people_exclusion_unavailable") else 422
    return UserFacing("style_refused", STYLE_REFUSED.get(code, STYLE_REFUSED["unreadable"]),
                      status)


def style_request(text: str) -> Optional[dict[str, str]]:
    """``{"kind", "author", "subject"}`` when the words ask for a document made like
    someone's (or the reader's) own; None otherwise. ``author`` is empty for the reader."""
    text = " ".join(str(text or "").split())
    if not _MAKE.search(text):
        return None
    kinds = ds.load_kinds()
    kind = ds.kind_of(text, kinds)
    if kind is None:
        return None
    like = _LIKE.search(text)
    if like is None and not re.search(r"\b(?:my|our)\s+(?:usual\s+)?(?:template|style|"
                                      r"format)\b", text, re.I):
        return None
    subject = _FOR.search(text)
    return {"kind": kind, "author": (like.group(1) or like.group(2)) if like else "",
            "subject": subject.group(1).strip(" ,.") if subject else ""}


def authors_named(name: str, authors: set[str]) -> list[str]:
    """The template authors a name means: a whole word of their address before the @, or
    the address itself."""
    name = name.lower().strip(" .'’")
    return sorted(a for a in authors if a == name or name in re.split(r"[._-]", a.split("@")[0]))


class DocStyleMixin:
    """Mixed into the backends after ``DocFillMixin`` (its people guard, upload desk and
    sources)."""

    def style_store(self) -> Optional[Path]:
        """The learned templates' directory (``templates.json`` and ``exemplars/``)."""
        return None

    def style_facts(self, reader: Reader, words: str) -> tuple[list[df.Fact], list[str],
                                                                Optional[m.ObjectRef]]:
        """Facts for the words, gathered as this reader reads (``doc_gather.gather``), notes
        on what was not read, and the first record matched."""
        return [], ["nothing read"], None

    def _style_usable(self, reader: Reader, template: Mapping[str, Any]) -> bool:
        for member in template["members"]:
            try:
                self.source(reader, member["source"], 0)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - refused, unknown or failed alike: not theirs
                return False
        return True

    def _style_template(self, reader: Reader, kind: str, author: str
                        ) -> Mapping[str, Any]:
        root = self.style_store()
        if root is None:
            raise _refuse("no_templates")
        try:
            templates = ds.load(root / "templates.json")
        except ds.StyleRefused as refused:
            raise _refuse(refused.code) from None
        of_kind = [t for t in templates if t["kind"] == kind]
        if author:
            named = authors_named(author, {t["author"] for t in of_kind})
        else:
            named = [reader.email.lower()] if reader.email else []
        usable = {}
        for who in named:
            mine = [t for t in of_kind if t["author"] == who]
            while mine:
                best = ds.choose(mine, who, kind)
                if best is None:
                    break
                if self._style_usable(reader, best):
                    usable[who] = best
                    break
                mine = [t for t in mine if t is not best]
        if len(usable) > 1:
            raise _refuse("two_authors")
        if not usable:
            raise _refuse("no_author")
        return next(iter(usable.values()))

    def style_make(self, reader: Reader, text: str, *, as_of: Optional[dt.date] = None
                   ) -> m.InstructFill:
        """A document in the template the words ask for, for this reader (module doc)."""
        from operating import instruct_files as files
        from .feedback import clean
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            raise _refuse("people_exclusion_unavailable")
        try:
            asked = style_request(text)
            terms = df.load_terms()
        except ds.StyleRefused as refused:
            raise _refuse(refused.code) from None
        except df.FillRefused:
            raise _refuse("templates_invalid") from None
        if asked is None:
            raise _refuse("no_kind")
        if not asked["subject"]:
            raise _refuse("no_subject")
        template = self._style_template(reader, asked["kind"], asked["author"])
        store = self.style_store()
        assert store is not None              # _style_template refused without one
        try:
            exemplar = (store / "exemplars" / template["exemplar"]["digest"]).read_bytes()
        except OSError:
            raise _refuse("exemplar_missing") from None
        facts, notes, ref = self.style_facts(reader, asked["subject"])
        # a gather cut short by the time limit: said first, so the draft reads as partial
        notes = [n for n in notes if n == dg.PARTIAL] + [n for n in notes if n != dg.PARTIAL]
        facts = [f for f in facts if clean(f.value, guard, len(f.value) + 1) == f.value]
        try:
            made = ds.generate(template, exemplar, facts, terms,
                               as_of=as_of or dt.date.today())
        except ds.StyleRefused as refused:
            raise _refuse(refused.code) from None
        label = ref.label if ref is not None else asked["subject"][:80]
        name = ds.made_name(template, label, made.format)
        people = sorted({a.lower() for u in made.units if u.status == "filled"
                         for a in re.findall(r"[\w.'+-]+@[\w-]+(?:\.[\w-]+)+", u.text)})
        row = self._uploads().prepare(  # type: ignore[attr-defined]
            reader.key, name=name, kind=made.format, data=made.data, target=None,
            people=people)

        def opens(handle: str) -> str:
            return f"/source?handle={quote(handle, safe='')}"
        status = {"kept": "kept", "filled": "filled", "dated": "filled"}
        shown = [m.FillField(
            label=clean(u.label, guard, 200) or "", before="",
            after=clean(u.text, guard, 400) or "",
            status=status.get(u.status) or ("ambiguous" if "differ" in u.reason or
                                            "disagree" in u.reason else "no_fact"),
            reason=u.reason, citation=m.FillCitation(
                record=u.citation.record, field=u.citation.field, source=u.citation.source,
                open_url=opens(u.citation.source)) if u.citation else None)
            for u in made.units if u.status != "removed" and u.role != "fixed"]
        counts = made.counts()
        return m.InstructFill(
            name=name, original=f"{template['kind'].replace('_', ' ')} template of "
                                f"{template['author'].split('@')[0]}",
            kind=made.format if made.format in ("docx", "xlsx") else "pdf",
            record=ref or m.ObjectRef(object_id="", type="Request", label=label,
                                      open_url=""),
            counts={"kept": counts["kept"], "filled": counts["filled"] + counts["dated"],
                    "no_fact": counts["blank"]},
            fields=shown, notes=list(notes)[:10],
            digest_before=hashlib.sha256(exemplar).hexdigest(), digest_after=made.digest,
            size=len(made.data), download_url=f"/api/instruct/files/{row['file_id']}")

    def style_answer(self, reader: Reader, text: str, context: Mapping[str, Any],
                     conversation: str, latency: int) -> m.AnswerPayload:
        """The Ask's answer to "make a quote for ... like ... does": the draft to review."""
        made = self.style_make(reader, text)
        n = made.counts
        partial = "Partial draft (the search ran out of time). " \
            if dg.PARTIAL in made.notes else ""
        blank = f", {n['no_fact']} left blank for you" if n["no_fact"] else ""
        said = (partial + f"Made {made.name} from {made.original}: {n['filled']} parts "
                f"filled from what you can read{blank}. Check each one against its source "
                "before you send it.")
        facts = [m.Fact(text=f"{f.label}: {f.after}", sources=[m.Source(
            system=system_of(f.citation.source), handle=f.citation.source,
            label=f"{f.label}"[:300], open_url=f.citation.open_url)])
            for f in made.fields if f.status == "filled" and f.citation]
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=text, outcome="answered",
            direct_answer=said, facts=facts, hidden=m.Hidden(count=0),
            freshness=m.Freshness(), subjects=[made.record] if made.record.object_id else [],
            conversation_id=conversation, latency_ms=latency, drafts=[],
            instruction=m.InstructPart(status="drafted", text=text, fills=[made],
                                       context=dict(context), read=[]))


__all__ = ["DocStyleMixin", "STYLE_REFUSED", "authors_named", "style_request"]
