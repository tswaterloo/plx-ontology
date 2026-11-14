"""Create mode in the Ask box: "create an order from this PO", "create a PO for SYN-10",
"enter this invoice". The Workbench pre-fills the record from the data
(``operating.entry_assist``) and answers with every field it filled, each with its source,
and every field left for the person to check, with the reason. No model, nothing written:
the person keys the record (Sage has no write path); a value is never invented.

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
        pasted = text.split(":", 1)[1].strip() if ":" in text else ""
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
        finally:
            st.close()
        return self._create_payload(reader, got, kind, text, context, conversation, latency,
                                    guard, clean)

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
