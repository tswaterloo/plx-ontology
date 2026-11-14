"""Create mode in the Ask box: "create an order from this PO", "create a PO for SYN-10",
"enter this invoice". The Workbench pre-fills the record from the data
(``operating.entry_assist``) and answers with every field it filled, each with its source,
and every field left for the person to check, with the reason. No model; a value is never
invented. The person keys the record in Sage (no write path there). For a reader who holds
the BC write grant, a fully cited order or PO is also put on the write desk as a new BC
order in the BC sandbox only (``_entry_write``: never Production, never a PHSI company,
never an AP invoice), prepared by that reader: a grant holder other than them approves it
there, and it stays "would write" until every switch of ``write_intents.desk_gate`` is set.

Where it reads from, as the reader: the mail open on the page (``context.handle``, opened
through ``source`` as this reader, else refused ``not_open``), the files attached to the
line, else the text after the line's colon. Every record a filled value comes from is
opened as the reader (``object_page``); a value from a record the reader cannot open
becomes a check ("from a record you cannot open"), never shown. Names and addresses of
excluded people are cleaned from everything shown (the people guard)."""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

from operating.entry_assist import create_kind

from . import models as m
from .auth import Reader
from .errors import UserFacing

LABELS = {"customer": "Customer", "po_number": "Customer PO", "ship_to": "Ship-to",
          "supplier": "Supplier", "vendor": "Vendor", "invoice_number": "Invoice number",
          "invoice_date": "Invoice date", "total": "Total", "purchase_order": "Purchase order",
          "item": "Item", "quantity": "Quantity", "unit": "Unit", "unit_price": "Unit price",
          "amount": "Amount"}
TITLES = {"order": "a Sage order", "purchase_order": "a purchase order",
          "supplier_invoice": "an AP invoice"}
REFUSED = {
    "store_unavailable": ("Create isn't available on this server: the records it fills from "
                          "are not served here.", 503),
    "nothing_to_read": ("Open the mail with the PO or invoice, attach the file, or paste its "
                        "text after a colon, and ask again.", 422),
    "not_open": ("That mail is not one you can open.", 404),
    "people_exclusion_unavailable": ("The people check is not available right now, so "
                                     "nothing is filled.", 503),
}


# the sandbox company the desk's BC header entries go to (the same setting as the
# autopilot's BC adapter); a company whose name says PHSI is never written
SANDBOX_COMPANY = "PLX_AUTOPILOT_BC_COMPANY_ID"
NEVER_COMPANY = re.compile(r"\bPHSI\b", re.I)
BC_OPERATION = {"order": "sales_order_create", "purchase_order": "purchase_order_create"}


def _refuse(code: str) -> UserFacing:
    message, status = REFUSED[code]
    return UserFacing("create_refused", message, status)


def _mail_oid(handle: str) -> Optional[str]:
    found = re.match(r"^graph-mail://([^/]+)/([^@]+)@", handle or "")
    if not found:
        return None
    return "Email:graph_mail.messages:" + json.dumps(
        {"id": found.group(2), "mailbox": found.group(1)}, separators=(",", ":"))


def _records_of(field: Mapping[str, Any]) -> list[str]:
    src = field.get("source") or {}
    out = [str(r) for r in (src.get("records") or ()) if r]
    if src.get("record"):
        out.append(str(src["record"]))
    if src.get("value_is_record") and isinstance(field.get("value"), str):
        out.append(field["value"])            # a value that is itself a record id
    return out


class EntryCreateMixin:
    """Mixed into the backends after ``InstructMixin`` (its people guard and sources)."""

    def _entry_ctx(self) -> Any:
        from pathlib import Path
        from operating import autopilot as ap
        path = getattr(self, "store_path", None)
        if not path or not Path(path).is_file():
            raise _refuse("store_unavailable")
        st = ap._store(str(path))
        from operating.time_window import business_today
        return st, ap.Ctx(st, business_today(), time.monotonic() + 60, ())

    def create_answer(self, reader: Reader, request: m.AskRequest, text: str,
                      context: Mapping[str, Any], conversation: str, kind: str,
                      latency: int) -> m.AnswerPayload:
        """The Ask's answer to a create line (module doc)."""
        from operating import entry_assist as ea
        from .feedback import clean
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            raise _refuse("people_exclusion_unavailable")
        handle = str(context.get("handle") or "")
        email = None
        if handle.startswith("graph-mail://") and kind != "purchase_order":
            try:
                self.source(reader, handle, 0)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - refused or failed: not the reader's
                raise _refuse("not_open") from None
            email = _mail_oid(handle)
        attached = self._instruct_attached(request, guard)  # type: ignore[attr-defined]
        # the text after the colon as typed: its lines are the PO's lines (``text`` is the
        # instruction with its whitespace folded to one line)
        raw = request.question if isinstance(request.question, str) and \
            request.instruction is None else text
        pasted = raw.split(":", 1)[1].strip() if ":" in raw else ""
        body = "\n\n".join([f.text for f in attached if getattr(f, "text", None)] +
                           ([pasted] if pasted else []))
        st, ctx = self._entry_ctx()
        try:
            if kind == "purchase_order":
                words = pasted or text
                items = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}", words)
                         if any(c.isdigit() for c in t)]
                got = ea.purchase_order(ctx, items=items)
            elif email is None and not body:
                raise _refuse("nothing_to_read")
            elif kind == "order":
                got = ea.order_entry(ctx, email=email if not body else None,
                                     text=body or None)
            else:
                got = ea.supplier_invoice(ctx, email=email if not body else None,
                                          text=body or None)
            proposal = ea.bc_header(ctx, got, kind) if kind != "supplier_invoice" else None
        finally:
            st.close()
        payload = self._create_payload(reader, got, kind, text, context, conversation,
                                       latency, guard, clean)
        write = self._entry_write(reader, proposal, kind) if proposal is not None else None
        if write is not None and payload.instruction is not None:
            payload.instruction.writes.append(write)
        return payload

    def _entry_write(self, reader: Reader, proposal: Mapping[str, Any],
                     kind: str) -> Optional[m.InstructWrite]:
        """The pre-fill's header as a dry-run entry on the write desk (BC Sandbox2 only,
        never Production, never PHSI Group), prepared by the reader when they hold the
        write grant for it (else None: nothing offered); when it cannot be, why not. Nothing is approved, confirmed or sent here: a grant
        holder other than the reader approves it on the desk, and it stays "would write"
        until every switch of ``write_intents.desk_gate`` is set."""
        from operating import write_intents as wi
        label = f"BC Sandbox: new {'sales' if kind == 'order' else 'purchase'} order header"

        def refused(*why: str) -> m.InstructWrite:
            return m.InstructWrite(label=label, status="refused", problems=list(why))

        operation = BC_OPERATION[kind]
        person = self._person(reader)  # type: ignore[attr-defined]
        ctx = self._write_ctx()  # type: ignore[attr-defined]
        if not ctx.granted().holds(person.ids(), "bc", wi.OPERATIONS[operation]["entity_set"]):
            return None
        if proposal.get("refused"):
            return refused(*proposal["refused"])
        # every value of the entry (party, items, prices) from records the reader opens:
        # the desk shows the entry to them and to the approver
        for oid in proposal["records"]:
            try:
                self.object_page(reader, oid.split(":", 1)[0], oid,  # type: ignore[attr-defined]
                                 quick=True)
            except Exception:  # noqa: BLE001 - not the reader's: nothing about it is shown
                return refused("A value of this entry comes from a record you cannot open, "
                               "so it is not put on the write desk.")
        env = self._writes_env()  # type: ignore[attr-defined]
        environment = str(env.get(wi.SANDBOX_ENV) or "").strip()
        if not environment or environment.lower() == wi.PRODUCTION.lower():
            return refused("No BC sandbox is configured on this server "
                           f"({wi.SANDBOX_ENV}); Production is never written from here.")
        company = str(env.get(SANDBOX_COMPANY) or "").strip().lower()
        if not wi._GUID.fullmatch(company):
            return refused(f"The sandbox company is not configured ({SANDBOX_COMPANY}).")
        name = self._sandbox_company_name(environment, company)
        if not name:
            return refused("The sandbox company could not be read from BC now, so "
                           "nothing is put on the write desk.")
        if NEVER_COMPANY.search(name):
            return refused("That company is never written from the Workbench.")
        party = proposal["party"]
        now = ctx.current(environment, company, proposal["entity_set"], party["bc_id"]) \
            if ctx.current else None
        if not now or not now.get("etag"):
            return refused(f"BC {party['number']} could not be read from the sandbox now, "
                           "so the entry cannot be checked.")
        try:
            found = self._wdesk().prepare({  # type: ignore[attr-defined]
                "system": "bc", "operation": proposal["operation"],
                "environment": environment, "company_id": company,
                "payload": dict(proposal["payload"]),
                "precondition": {"etag": now["etag"], "party_id": party["bc_id"]},
                "reason": proposal["reason"], "citations": list(proposal["citations"])},
                person)
            intent_id = found["intent"]["intent_id"]
            preview = self.write_preview(reader, intent_id)  # type: ignore[attr-defined]
        except wi.WriteRefused as no:
            return refused(*no.problems)
        except UserFacing as no:
            return refused(no.message)
        return m.InstructWrite(intent_id=intent_id, label=label, status="prepared",
                               changes=preview.changes, problems=preview.problems)

    write_company: Any = None   # tests: (environment, company_id) -> name | None

    def _sandbox_company_name(self, environment: str, company: str) -> Optional[str]:
        """The company's name in ``environment``, read now through the desk's GET-only BC
        client (None: unreadable here, or another environment than the client's)."""
        if self.write_company is not None:
            return self.write_company(environment, company)
        from integrations.live_source_guard import (NotVisible, SourceUnavailable)
        from ontology.core import Actor
        try:
            client, env_name = self._bc_client()  # type: ignore[attr-defined]
            if env_name.lower() != environment.lower():
                return None
            got = client.companies(Actor("writes", frozenset({"writes:preview"}),
                                         "restricted"))
        except (SourceUnavailable, NotVisible, ValueError, KeyError, TypeError):
            return None
        for item in got.get("items") or ():
            if str(item.get("id") or "").lower() == company:
                return str(item.get("name") or item.get("display_name") or "") or None
        return None

    def _create_payload(self, reader: Reader, got: Mapping[str, Any], kind: str, text: str,
                        context: Mapping[str, Any], conversation: str, latency: int,
                        guard: Any, clean: Any) -> m.AnswerPayload:
        opened: dict[str, Optional[str]] = {}

        def label_of(oid: str) -> Optional[str]:
            """The record's label when the reader can open it, else None."""
            if oid not in opened:
                otype = oid.split(":", 1)[0]
                try:
                    page = self.object_page(reader, otype, oid,  # type: ignore[attr-defined]
                                            quick=True)
                    opened[oid] = clean(page.label, guard, 200) or otype
                except Exception:  # noqa: BLE001 - not the reader's: never shown
                    opened[oid] = None
            return opened[oid]

        facts: list[m.Fact] = []
        checks: list[str] = []

        def show(name: str, field: Mapping[str, Any], where: str = "") -> None:
            label = LABELS.get(name, name) + (f" ({where})" if where else "")
            if field.get("status") == "cited":
                recs = _records_of(field)
                if any(label_of(r) is None for r in recs):
                    checks.append(f"{label}: from a record you cannot open")
                    return
                value = field["value"]
                shown = label_of(value) if isinstance(value, str) and value in opened \
                    else value
                src = field.get("source") or {}
                per = " ".join(str(src[k]) for k in ("currency", "unit") if src.get(k))
                said = clean(f"{label}: {shown}" + (f" {per}" if per else ""), guard, 300) or ""
                if not said:
                    checks.append(f"{label}: withheld")
                    return
                src = field.get("source") or {}
                sources = [m.Source(system="Portal", handle=r, label=opened[r] or r,
                                    open_url=f"/object/{quote(r.split(':', 1)[0], safe='')}/"
                                             f"{quote(r, safe='')}") for r in recs[:3]]
                if not sources:
                    doc = clean(str(src.get("document") or "the document"), guard, 120)
                    sources = [m.Source(system="File", handle=str(src.get("handle") or ""),
                                        label=f"{doc} {src.get('cite') or ''}".strip())]
                facts.append(m.Fact(text=said, sources=sources))
            else:
                why = clean(str(field.get("reason") or ""), guard, 200) or "not in the records"
                hint = ""
                for s in field.get("suggestions") or ():
                    # a suggestion's records are opened as the reader too: any one they
                    # cannot open and the suggestion is not shown
                    recs = [str(r) for r in (s.get("records") or ()) if r] + \
                        ([str(s["record"])] if s.get("record") else []) + \
                        ([str(s["value"])] if s.get("value_is_record") else [])
                    if not recs or any(label_of(r) is None for r in recs):
                        continue
                    value = label_of(str(s["value"])) if s.get("value_is_record") \
                        else s["value"]
                    hint += "; " + (clean(f"suggested {value} ({s['basis']})", guard,
                                          160) or "")
                checks.append(f"{label}: {why}{hint}")

        for name, field in (got.get("header") or {}).items():
            show(name, field)
        for i, ln in enumerate(got.get("lines") or (), 1):
            for name, field in ln.items():
                if name != "text":
                    show(name, field, f"line {i}")
        filled = len(facts)
        said = (f"Pre-filled {TITLES[kind]}: {filled} field{'s' if filled != 1 else ''} from "
                f"the records, {len(checks)} for you to check. Nothing is entered: key it "
                "yourself after checking each value against its source.")
        if checks:
            said += "\n" + "\n".join(f"Check - {c}" for c in checks[:60])
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=text, outcome="answered",
            direct_answer=said, facts=facts, hidden=m.Hidden(count=0),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=latency,
            drafts=[], instruction=m.InstructPart(
                status="drafted", text=text, context=dict(context),
                read=[m.InstructRead(kind="record", label=TITLES[kind][:200])]))


__all__ = ["EntryCreateMixin", "create_kind", "REFUSED"]
