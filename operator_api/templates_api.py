"""Saved templates for one reader (``operator_api.templates``): list, save, delete, turn a
draft into a template, fill one from a record, and offer one when a document of its type is
started; and the one-box's "use my quote template for SO 1234".

* **Sharing**: a template's owner shares it with anyone: a colleague by address, or a
  role of the roles file (``autopilot._roles``, derived from what people did, never a list
  of names). A reader belongs to a role by their object id. ``person:`` roles are people,
  not roles: never offered (share with the address). A roles file that cannot be read
  gives no roles: nothing shared through a role is listed, and no role can be named.
* **The record** is named as a form fill names it (``DocFillMixin._fill_record``: the page's
  record, an id, or the one record an identifier names exactly; two: refused), and read as
  the reader reads it (``record_facts``, ``record_lines``).
* **Learned suggestions** come from the layouts ``operating.doc_style_mine`` learned from
  the documents the reader sent (``DocStyleMixin``), usable only when the reader may still
  open every document it was learned from.

Nothing here sends or writes outside the reader's own templates. Evidence label of tests:
``isolated-fixture``.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import Any, Mapping, Optional
from urllib.parse import quote

from operating import doc_fill as df

from . import feedback as fb
from . import models as m
from . import templates as tp
from .auth import Reader
from .errors import UserFacing

log = logging.getLogger("operator_api.templates")

# a template type -> the learned document kind of the same documents
LEARNED_KIND = {"quote": "quote", "price_list": "price_list", "proposal": "proposal",
                "spec_sheet": "spec_sheet", "coa": "coa", "letter": "letter"}


def refused(error: tp.TemplateRefused) -> UserFacing:
    status = {"not_found": 404, "view_as": 403, "not_kept": 403,
              "people_exclusion_unavailable": 503, "unavailable": 503,
              "name_taken": 409}.get(error.code, 422)
    return UserFacing("template_refused", error.message, status)


class TemplatesMixin:
    """Mixed into the backends after ``DocStyleMixin`` (records, people guard, learned
    layouts)."""

    _template_store: Any = None

    def template_store(self) -> Optional[tp.TemplateStore]:
        """The store over the backend's Postgres (beside memory); None: templates are off."""
        if self._template_store is None:
            self._template_store = tp.TemplateStore.for_backend(self)
        return self._template_store

    def _store_or_refuse(self) -> tp.TemplateStore:
        store = self.template_store()
        if store is None:
            raise refused(tp.TemplateRefused("unavailable"))
        return store

    def template_roles(self, reader: Reader) -> tuple[set[str], set[str]]:
        """(roles the reader belongs to, roles there are to share with); both empty when
        the roles cannot be read."""
        try:
            from operating.proactive_commitments import owner_ids
            from .autopilot_area import _principals
            roles = self._apa_roles()  # type: ignore[attr-defined]
            mine = owner_ids(_principals(reader))
        except Exception as error:  # noqa: BLE001 - unreadable: no role, nothing shared
            log.warning("template roles unreadable: %s", type(error).__name__)
            return set(), set()
        named = {r for r in roles if not r.startswith("person:") and tp._ROLE.match(r)}
        member = {r for r in named if mine & {str(o).lower() for o in roles[r]}}
        return member, named

    # -- list, save, delete --

    def templates_list(self, reader: Reader, doc_type: Optional[str] = None) -> m.TemplateList:
        kinds = [m.TemplateKind(doc_type=k, label=v) for k, v in tp.KINDS.items()]
        try:
            fields = [m.TemplatePlaceholder(**f) for f in tp.known_fields(df.load_terms())]
        except df.FillRefused:
            fields = []
        store = self.template_store()
        if store is None:
            return m.TemplateList(available=False, kinds=kinds, fields=fields)
        member, shareable = self.template_roles(reader)
        rows = store.list(reader, roles=member, doc_type=doc_type)
        if rows is None:
            return m.TemplateList(available=False, kinds=kinds, fields=fields)
        return m.TemplateList(available=True, templates=[m.Template(**r) for r in rows],
                              kinds=kinds, fields=fields, share_roles=sorted(shareable))

    def template_save(self, reader: Reader, body: m.TemplateSave,
                      template_id: Optional[int] = None) -> m.Template:
        store = self._store_or_refuse()
        _member, shareable = self.template_roles(reader)
        try:
            row = store.save(reader, name=body.name, doc_type=body.doc_type, body=body.body,
                             style_notes=body.style_notes, shared_with=body.shared_with,
                             source_ref=body.source_ref, template_id=template_id,
                             shareable=shareable, terms=df.load_terms())
        except tp.TemplateRefused as error:
            raise refused(error) from None
        except df.FillRefused:
            raise UserFacing("template_refused", "Templates are paused: the list of record "
                             "fields could not be read.", 503) from None
        return m.Template(**row)

    def template_delete(self, reader: Reader, template_id: int) -> bool:
        store = self.template_store()
        return bool(store is not None and store.delete(reader, template_id))

    # -- records --

    def record_lines(self, reader: Reader, object_type: str, object_id: str
                     ) -> Optional[list[list[df.Fact]]]:
        """An order's lines' facts as the reader reads them; None: not read (the real
        backend reads them)."""
        return None

    def _template_record(self, reader: Reader, words: Optional[str],
                         context: Mapping[str, Any]) -> Optional[tuple[str, str]]:
        if not (words or "").strip() and not (context.get("kind") == "record"
                                              and context.get("object_id")):
            return None
        return self._fill_record(reader, words or "", context)  # type: ignore[attr-defined]

    def _template_facts(self, reader: Reader, named: tuple[str, str]
                        ) -> tuple[list[df.Fact], list[str], m.ObjectRef]:
        try:
            return self.record_facts(reader, *named)  # type: ignore[attr-defined]
        except (KeyError, LookupError, PermissionError):
            raise UserFacing("template_refused", "That record is not one you can open.",
                             404) from None
        except df.FillRefused:
            raise UserFacing("template_refused", "The record could not be read in full, so "
                             "nothing was filled.", 503) from None

    # -- propose, fill --

    def template_propose(self, reader: Reader, request: m.TemplateProposeRequest
                         ) -> m.TemplateProposal:
        """A draft's text with the record's exact values as placeholders (nothing saved)."""
        named = self._template_record(reader, request.record, {})
        if named is None:
            return m.TemplateProposal(body=request.text)
        facts, _notes, ref = self._template_facts(reader, named)
        got = tp.propose(request.text, facts, df.load_terms())
        return m.TemplateProposal(body=got["body"], record=ref, replaced=[
            m.TemplateReplaced(**r) for r in got["replaced"]])

    def template_fill(self, reader: Reader, template_id: int, record: Optional[str], *,
                      context: Optional[Mapping[str, Any]] = None,
                      today: Optional[dt.date] = None) -> m.TemplateFilled:
        """One of the reader's templates (or one shared with them) filled from the record:
        every placeholder from one value the records agree on, cited, else marked check."""
        store = self._store_or_refuse()
        member, _shareable = self.template_roles(reader)
        row = store.get(reader, template_id, roles=member)
        if row is None:
            raise refused(tp.TemplateRefused("not_found"))
        return self._filled(reader, store, row, record, context or {}, member, today)

    def _filled(self, reader: Reader, store: tp.TemplateStore, row: Mapping[str, Any],
                record: Optional[str], context: Mapping[str, Any], member: set[str],
                today: Optional[dt.date]) -> m.TemplateFilled:
        terms = df.load_terms()
        named = self._template_record(reader, record, context)
        facts: list[df.Fact] = []
        notes: list[str] = []
        ref: Optional[m.ObjectRef] = None
        lines: Optional[list[list[df.Fact]]] = None
        if named is not None:
            facts, notes, ref = self._template_facts(reader, named)
            if any(p["name"] == "items" for p in row["placeholders"]):
                lines = self.record_lines(reader, *named)  # type: ignore[attr-defined]
        else:
            notes.append("No record named: every placeholder is marked check.")
        got = tp.fill(row["body"], facts, terms, lines=lines,
                      today=today or dt.datetime.now().date())
        store.used(reader, row["id"], roles=member)
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        fields = []
        for f in got["fields"]:
            # a value naming a person the exclusion keeps out never enters the text
            if f["status"] == "filled" and (guard is None or fb.WITHHELD in (
                    fb.clean(f["value"], guard, len(f["value"]) + 1) or "")):
                got["text"] = got["text"].replace(f["value"], tp._check(f["label"]))
                f.update(status="check", value="", citation=None, also=[],
                         reason="withheld")
            fields.append(m.TemplateFillField(
                name=f["name"], label=f["label"], type=f["type"], status=f["status"],
                value=f["value"], reason=f["reason"],
                citation=_citation(f["citation"]), also=[
                    c for c in (_citation(a) for a in f["also"][:10]) if c]))
        return m.TemplateFilled(
            template_id=row["id"], name=row["name"], doc_type=row["doc_type"],
            text=got["text"], style_notes=row["style_notes"], record=ref, fields=fields,
            checks=sum(1 for f in fields if f.status == "check"), notes=notes[:10])

    # -- suggestions --

    def template_suggest(self, reader: Reader, doc_type: str) -> m.TemplateSuggestions:
        """What to offer when the reader starts a document of ``doc_type``: their own and
        shared templates of it, most used first, then the layout learned from what they
        sent of that kind."""
        if doc_type not in tp.KINDS:
            raise refused(tp.TemplateRefused("doc_type_unknown"))
        out: list[m.TemplateSuggestion] = []
        store = self.template_store()
        if store is not None:
            member, _shareable = self.template_roles(reader)
            for row in (store.list(reader, roles=member, doc_type=doc_type) or ())[:5]:
                used = f"used {row['uses']} times" if row["uses"] else "not used yet"
                whose = "yours" if row["mine"] else "shared with you"
                out.append(m.TemplateSuggestion(kind="saved", doc_type=doc_type,
                                                label=row["name"], detail=f"{whose}, {used}",
                                                template=m.Template(**row)))
        learned = self._learned(reader, doc_type)
        if learned is not None:
            out.append(learned)
        return m.TemplateSuggestions(doc_type=doc_type, suggestions=out)

    def _learned(self, reader: Reader, doc_type: str) -> Optional[m.TemplateSuggestion]:
        kind = LEARNED_KIND.get(doc_type)
        if kind is None or not reader.email or getattr(reader.session, "view_as", False):
            return None
        try:
            best = self._style_template(reader, kind, "")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - none learned, none usable, or unreadable: none
            return None
        n = len(best.get("members") or ())
        words = tp.KINDS[doc_type].lower()
        return m.TemplateSuggestion(
            kind="learned", doc_type=doc_type, label=f"Your usual {words} layout",
            detail=f"learned from {n} you sent", ask=f"make a {words} for ")

    # -- the one-box --

    def template_answer(self, reader: Reader, text: str, context: Mapping[str, Any],
                        conversation: str, latency: int) -> Optional[m.AnswerPayload]:
        """The one-box's "use my <name> template for <record>": the filled template to
        review. None: no template of the reader's goes by that name or type (the line is
        then worked as any other)."""
        asked = tp.template_request(text)
        store = self.template_store()
        if asked is None or store is None:
            return None
        member, _shareable = self.template_roles(reader)
        rows = store.list(reader, roles=member)
        if rows is None:
            if getattr(reader.session, "view_as", False):
                return None     # nothing of theirs is read while viewing as them
            raise refused(tp.TemplateRefused("people_exclusion_unavailable"))
        found = tp.named(rows, asked["name"])
        if not found:
            return None
        if len(found) > 1:
            names = "; ".join(r["name"] for r in found[:4])
            said = f"You have more than one of those: {names}. Say which one by its name."
            filled = None
        else:
            filled = self._filled(reader, store, found[0], asked["for"], context, member, None)
            check = (f" {filled.checks} part{'s' if filled.checks != 1 else ''} marked "
                     "check: no record you can read settles them.") if filled.checks else ""
            said = (f"Filled your template \"{filled.name}\""
                    + (f" from {filled.record.label}." if filled.record else ".") + check
                    + " Read it through before you use it; nothing was sent.")
        facts = [m.Fact(text=f"{f.label}: {f.value}"[:400], sources=[m.Source(
            system=_system(f.citation.source), handle=f.citation.source,
            label=f.label[:300], open_url=f.citation.open_url)])
            for f in (filled.fields if filled else []) if f.status == "filled" and f.citation]
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=text, outcome="answered",
            direct_answer=said, facts=facts, hidden=m.Hidden(count=0),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=latency,
            subjects=[filled.record] if filled and filled.record else [], drafts=[],
            instruction=m.InstructPart(status="drafted", text=text, template=filled,
                                       context=dict(context), read=[]))


def _citation(c: Optional[Mapping[str, str]]) -> Optional[m.FillCitation]:
    if not c or not c.get("source"):
        return None
    return m.FillCitation(record=c["record"], field=c["field"], source=c["source"],
                          open_url=f"/source?handle={quote(c['source'], safe='')}")


def _system(handle: str) -> str:
    from .doc_fill_api import system_of
    return system_of(handle)


__all__ = ["TemplatesMixin", "refused"]
