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
excluded people are cleaned from everything shown (the people guard).

An order or PO the reader may prepare that is held by its checks (the party, or a line's
item, quantity or price) comes back as ``instruction.entry``: the reader settles each check
(``entry_confirm``, ``operating.entry_confirm``), and once none is left it goes to the write
desk exactly as a fully cited one. Only the person who prepared it sees or confirms it, a
view-as session never; confirming writes nothing to BC. A value from a record the reader
cannot open (or one the people guard withholds) is never theirs to settle: the entry is
not held for them, and a held one they can no longer see refuses every confirmation
(``not_visible``, 409), so nobody overrides what they cannot see."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import time
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import quote

from operating import entry_confirm as ecf
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


def _preparer(reader: Reader) -> Optional[str]:
    """The reader's own object id, the key their held entries are kept under (None: a
    session naming nobody, or a view-as one: nothing is held or confirmed for it)."""
    session = getattr(reader, "session", None)
    if getattr(session, "view_as", False) or getattr(session, "warm_ahead", False):
        return None
    oid = str(getattr(session, "subject_oid", "") or "").strip().lower()
    return oid or None


UNSEEN = ("A value of this entry comes from a record you cannot open, so it is not yours "
          "to settle: someone who can open that record prepares it.")
EXPIRED = f"This entry was held more than {ecf.HOLD_DAYS} days ago: ask for it again."


@contextlib.contextmanager
def _locked(holds: ecf.EntryHolds) -> Iterator[None]:
    """``holds.lock`` for one request; a store still locked after ``ecf.LOCK_WAIT``
    seconds answers 503 "try again" rather than waiting on."""
    try:
        holds.lock.__enter__()
    except ecf.Busy as busy:
        raise UserFacing(busy.code, busy.message, busy.status) from None
    try:
        yield
    finally:
        holds.lock.__exit__(None, None, None)


def _hold_label(kind: str) -> str:
    return f"New BC {'sales' if kind == 'order' else 'purchase'} order, held until " \
        "each check is settled"


class EntryCreateMixin:
    """Mixed into the backends after ``InstructMixin`` (its people guard and sources)."""

    def _entry_ctx(self) -> Any:
        from pathlib import Path
        from operating import autopilot as ap
        from operating import autopilot_handlers
        path = getattr(self, "store_path", None)
        if not path or not Path(path).is_file():
            raise _refuse("store_unavailable")
        st = ap._store(str(path))
        from operating.time_window import business_today
        return st, autopilot_handlers.Ctx(st, business_today(), time.monotonic() + 60, ())

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
            preparer = _preparer(reader)
            if write.status == "refused" and preparer and kind in ecf.PARTY:
                # held by its checks: the reader settles them (``entry_confirm``)
                fields, left, unseen = self._entry_fields(reader, got, kind, guard, clean)
                if unseen:
                    # a value the reader may not see is never theirs to settle or override
                    write.problems.append(UNSEEN)
                elif left and len(got.get("lines") or ()) <= ecf.MAX_LINES:
                    try:
                        entry_id = self._holds().hold(preparer, kind, got)
                    except ecf.Busy as busy:
                        write.problems.append(busy.message)
                        return payload
                    payload.instruction.entry = m.EntryHold(
                        entry_id=entry_id, kind=kind, label=_hold_label(kind),
                        fields=fields, open=left)
        return payload

    # ------------------------------------------------------------- held entries

    entry_holds: Any = None     # tests: an ``ecf.EntryHolds``

    def _holds(self) -> ecf.EntryHolds:
        if self.entry_holds is None:
            self.entry_holds = ecf.EntryHolds.for_state_dir(getattr(self, "state_dir", None))
        return self.entry_holds

    def _may_prepare(self, reader: Reader, kind: str) -> bool:
        """Whether the reader holds the BC write grant this entry is prepared under."""
        from operating import write_intents as wi
        person = self._person(reader)  # type: ignore[attr-defined]
        return bool(self._write_ctx().granted().holds(  # type: ignore[attr-defined]
            person.ids(), "bc", wi.OPERATIONS[BC_OPERATION[kind]]["entity_set"]))

    def _own_hold(self, reader: Reader, entry_id: str) -> tuple[str, dict[str, Any], Any]:
        """The reader's own held entry, with the people guard: a view-as session, someone
        else's entry or an unknown one is refused (never told apart), and so is a reader
        who no longer holds the grant."""
        session = getattr(reader, "session", None)
        if getattr(session, "view_as", False) or getattr(session, "warm_ahead", False):
            raise UserFacing("forbidden", "Viewing as someone is read only: nothing is "
                             "confirmed as them.", 403)
        preparer = _preparer(reader)
        try:
            hold = self._holds().get(preparer, entry_id) if preparer else None
        except ecf.Busy as busy:
            raise UserFacing(busy.code, busy.message, busy.status) from None
        if hold is None:
            raise UserFacing("not_found", "No such entry of yours.", 404)
        if not self._may_prepare(reader, hold["kind"]):
            raise UserFacing("forbidden", "You no longer hold the write grant this entry "
                             "is prepared under.", 403)
        if hold.get("pruned"):
            # only its preparer gets here (anyone else had the 404 above)
            if hold["intent_id"]:
                raise UserFacing("entry_prepared", "This entry is already on the write "
                                 "desk; nothing on it changes here.", 409)
            raise UserFacing("entry_expired", EXPIRED, 409)
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            raise _refuse("people_exclusion_unavailable")
        return preparer, hold, guard

    def entry_view(self, reader: Reader, entry_id: str) -> m.EntryHold:
        """A held entry as its preparer sees it now (module doc)."""
        from .feedback import clean
        _who, hold, guard = self._own_hold(reader, entry_id)
        return self._hold_view(reader, hold, guard, clean, None)

    def entry_confirm(self, reader: Reader, entry_id: str,
                      body: m.EntryConfirm) -> m.EntryHold:
        """The reader enters or confirms one value of their held entry (module doc); with
        none left, the entry goes to the write desk as theirs (``_entry_write``). Nothing
        is written to BC, approved or sent here."""
        from .feedback import clean
        from operating import entry_assist as ea
        holds = self._holds()
        with _locked(holds):
            preparer, hold, guard = self._own_hold(reader, entry_id)
            kind = hold["kind"]
            if hold["intent_id"]:
                raise UserFacing("entry_prepared", "This entry is already on the write "
                                 "desk; nothing on it changes here.", 409)
            if hold["expired"]:
                raise UserFacing("entry_expired", EXPIRED, 409)
            try:
                name, line = ecf.parse_key(kind, hold["prefill"], body.key)
                fields, _left, unseen = self._entry_fields(reader, hold["prefill"], kind,
                                                           guard, clean)
                if unseen:
                    raise ecf.Refused("not_visible", UNSEEN, 409)
                now = next((f for f in fields if f.key == body.key), None)
                if now is None or now.status == "cited":
                    raise ecf.Refused("cited", "That value is cited from the records; it "
                                      "is not changed here.", 409)
                field = self._confirmed_value(reader, name, body)
            except ecf.Refused as no:
                raise UserFacing(no.code, no.message, no.status) from None
            holds.confirm(preparer, entry_id, body.key, field)
            hold = holds.get(preparer, entry_id) or hold
            fields, left, unseen = self._entry_fields(reader, hold["prefill"], kind, guard,
                                                      clean)
            write = None
            if not left and not unseen:
                st, ctx = self._entry_ctx()
                try:
                    proposal = ea.bc_header(ctx, hold["prefill"], kind)
                finally:
                    st.close()
                write = self._entry_write(reader, proposal, kind)
                if write is not None and write.status == "prepared" and write.intent_id:
                    holds.prepared(preparer, entry_id, write.intent_id)
            return m.EntryHold(entry_id=entry_id, kind=kind, label=_hold_label(kind),
                               fields=fields, open=left, write=write)

    def _confirmed_value(self, reader: Reader, name: str,
                         body: m.EntryConfirm) -> dict[str, Any]:
        """The person's value for field ``name``, checked (else ``ecf.Refused``): a
        record of the field's type they open, with its number; a quantity above zero; a
        price of zero or more with its currency."""
        person = self._person(reader)  # type: ignore[attr-defined]
        by = {"name": reader.display_name or "", "email": person.email or "",
              "oid": str(reader.session.subject_oid or "")}
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc)
        if name == "quantity":
            return ecf.confirmed(ecf.number(body.value, positive=True), by=by, at=at)
        if name == "unit_price":
            return ecf.confirmed(ecf.number(body.value, positive=False), by=by, at=at,
                                 currency=ecf.currency(body.currency))
        oid = str(body.record or "").strip()
        otype = ecf.RECORD_TYPE[name]
        if not oid.startswith(otype + ":"):
            raise ecf.Refused("bad_record", f"Pick a {otype.lower()} record.", 422)
        try:
            self.object_page(reader, otype, oid, quick=True)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - not the reader's: never used, never told apart
            raise ecf.Refused("not_found", "That is not a record you can open.",
                              404) from None
        if name == "supplier":
            return ecf.confirmed(oid, by=by, at=at, records=[oid], value_is_record=True)
        from operating import autopilot as ap
        st, ctx = self._entry_ctx()
        try:
            row = ctx.read([oid], ("erp_code", "code", "name")).get(oid) or {}
        finally:
            st.close()
        code = (ap._v(row, "erp_code") or ap._v(row, "code")) if name == "customer" \
            else ap._v(row, "code")
        if not code:
            raise ecf.Refused("bad_record", f"That {otype.lower()} record has no number, so "
                              "BC cannot take it.", 422)
        return ecf.confirmed(code, by=by, at=at, records=[oid])

    def _entry_fields(self, reader: Reader, prefill: Mapping[str, Any], kind: str,
                      guard: Any, clean: Any) -> tuple[list[m.EntryField], int, list[str]]:
        """Each value the BC entry needs as the reader may see it, how many are still
        checks, and the keys of those the reader may not see: a cited or confirmed value
        from a record the reader cannot open, or one the people guard withholds, is a
        check that is not shown and that this reader can never settle (``UNSEEN``: the
        entry is not theirs to prepare, never overridden by a value of theirs);
        suggestions only from records they open."""
        unseen: list[str] = []
        opened: dict[str, Optional[str]] = {}

        def label_of(oid: str) -> Optional[str]:
            if oid not in opened:
                try:
                    page = self.object_page(  # type: ignore[attr-defined]
                        reader, oid.split(":", 1)[0], oid, quick=True)
                    opened[oid] = clean(page.label, guard, 200) or oid.split(":", 1)[0]
                except Exception:  # noqa: BLE001 - not the reader's: never shown
                    opened[oid] = None
            return opened[oid]

        out: list[m.EntryField] = []
        for key, name, line in ecf.held_keys(prefill, kind):
            field = ecf.field_at(prefill, key) or {}
            label = LABELS.get(name, name) + (f" (line {line})" if line else "")
            status = field.get("status")
            src = field.get("source") or {}
            if status in ("cited", "confirmed"):
                recs = _records_of(field)
                if any(label_of(r) is None for r in recs):
                    out.append(m.EntryField(key=key, field=name, line=line, label=label,
                                            status="check",
                                            reason="from a record you cannot open"))
                    unseen.append(key)
                    continue
                value = field.get("value")
                shown = label_of(value) if isinstance(value, str) and value in opened \
                    and src.get("value_is_record") else value
                if name == "customer" and recs and status == "confirmed":
                    shown = f"{value} {label_of(recs[0]) or ''}".strip()
                said = clean(str(shown), guard, 200) if shown is not None else None
                if not said:
                    out.append(m.EntryField(key=key, field=name, line=line, label=label,
                                            status="check", reason="withheld"))
                    unseen.append(key)
                    continue
                out.append(m.EntryField(
                    key=key, field=name, line=line, label=label, status=status, value=said,
                    currency=str(src["currency"])[:3] if src.get("currency") else None,
                    confirmed_by=(clean(str(src.get("confirmed_by") or ""), guard, 120)
                                  or None) if status == "confirmed" else None,
                    confirmed_at=(str(src.get("confirmed_at") or "") or None)
                    if status == "confirmed" else None))
                continue
            options: list[m.EntryOption] = []
            for s in field.get("suggestions") or ():
                recs = [str(r) for r in (s.get("records") or ()) if r] + \
                    ([str(s["record"])] if s.get("record") else []) + \
                    ([str(s["value"])] if s.get("value_is_record") else [])
                if not recs or any(label_of(r) is None for r in recs):
                    continue
                record = None
                if name in ecf.RECORD_TYPE:
                    record = next((r for r in recs if r.startswith(
                        ecf.RECORD_TYPE[name] + ":")), None)
                    if record is None:
                        continue        # nothing the person could pick
                value = label_of(str(s["value"])) if s.get("value_is_record") else s["value"]
                said = clean(str(value), guard, 160)
                basis = clean(str(s.get("basis") or ""), guard, 160) or "suggested"
                if not said:
                    continue
                options.append(m.EntryOption(
                    value=said, record=record, basis=basis,
                    currency=str(s["currency"])[:3] if s.get("currency") else None,
                    unit=clean(str(s["unit"]), guard, 20) if s.get("unit") else None,
                    date=str(s["date"])[:10] if s.get("date") else None))
            out.append(m.EntryField(
                key=key, field=name, line=line, label=label, status="check",
                reason=clean(str(field.get("reason") or ""), guard, 200) or
                "not in the records", options=options[:6]))
        return out, sum(1 for f in out if f.status == "check"), unseen

    def _hold_view(self, reader: Reader, hold: Mapping[str, Any], guard: Any, clean: Any,
                   write: Optional[m.InstructWrite]) -> m.EntryHold:
        fields, left, _unseen = self._entry_fields(reader, hold["prefill"], hold["kind"],
                                                   guard, clean)
        if write is None and hold.get("intent_id"):
            label = f"BC Sandbox: new {'sales' if hold['kind'] == 'order' else 'purchase'} " \
                "order header"
            try:
                preview = self.write_preview(  # type: ignore[attr-defined]
                    reader, hold["intent_id"])
                write = m.InstructWrite(intent_id=hold["intent_id"], label=label,
                                        status="prepared", changes=preview.changes,
                                        problems=preview.problems)
            except UserFacing as no:
                write = m.InstructWrite(label=label, status="refused", problems=[no.message])
        return m.EntryHold(entry_id=hold["entry_id"], kind=hold["kind"],
                           label=_hold_label(hold["kind"]), fields=fields, open=left,
                           write=write)

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
                    # a money hint says its currency and the unit it is per, and when
                    per = (f" {s['currency']}" if s.get("currency") else "") + \
                        (f" per {s['unit']}" if s.get("unit") and s.get("currency") else "")
                    when = f", {s['date']}" if s.get("date") else ""
                    hint += "; " + (clean(f"suggested {value}{per} ({s['basis']}{when})",
                                          guard, 160) or "")
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
