"""The autopilot handlers that prepare a whole piece of work from the records (the engine,
queue and safety are ``operating.autopilot``'s):

* ``order_entry`` (customer PO mail): the PO's lines read from its attachments or body
  (``document_lines``), the customer resolved (the mail's customer link, the records
  stating the sender's address, else its domain, twins and ship-tos of one bill-to folded
  together, a name or customer number the PO itself states), each line's item through
  the identifier table (exactly one item number, else the line is not resolved), the
  customer's last invoiced (else ordered) price per item, the item's available date where
  stock or open PO lines state one (else "not checked"), then a dry-run ``sales_order_create``
  write intent only when the customer and every line resolved, and a confirmation draft.
* ``po_followup`` (an open PO with no supplier confirmation, ETA or reply ``after_days``
  after it was ordered): a follow-up drafted to the supplier contact the PO went to,
  citing its lines and dates.
* ``supplier_eta`` (a supplier's reply naming our PO and stating a date): the ETA change
  recorded against the PO's expected date and, on a slip, the open customer orders that
  use its items.
* ``desk`` (a direct ask to the owner's mailbox): a drafted answer when the records the
  ask names answer it, else a decision card (approval, price, finance, escalation) with a
  recommendation and its evidence, else nothing (an item never restates the ask).

Drafts are short and in the company's measured voice: ``Hi <first name>,`` or no greeting,
no sign-off, through the copy gate (``copy_gate.settle``: an optional sentence that fails is
dropped, a required one that fails leaves no draft). Nothing here sends or writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ontology.measure import money_text

from . import autopilot as ap
from . import document_lines as dl

ITEM_KINDS = ("item_code", "erp_item_code")
DONT_USE = re.compile(r"\b(?:do ?n[o']?t use|inactive|obsolete|closed|duplicate)\b", re.I)
MAX_ATTACHMENTS = 4
PRICE_INVOICES = 60
FIRST_NAME = re.compile(r"^[A-Za-z][a-z]{1,15}$")


# ----------------------------------------------------------------------------- texts

def _handle(ctx: Any, oid: str) -> str | None:
    row = ctx.st.conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? AND "
                              "superseded_at IS NULL", (oid,)).fetchone()
    return row[0] if row else None


def _store_text(ctx: Any, handle: str, path: str) -> str | None:
    from .mail_text import joined_chunks
    try:
        rows = ctx.st.conn.execute(
            "SELECT k.start, k.body FROM text_fields f JOIN text_chunks k "
            "ON k.field_id = f.field_id WHERE f.citation = ? AND f.field_path = ? "
            "ORDER BY k.start", (handle, path)).fetchall()
    except sqlite3.Error:
        return None
    try:
        # chunks overlap (``text_index.CHUNK_OVERLAP``): each laid at its own offset
        return joined_chunks(rows) or None
    except Exception:  # noqa: BLE001 - an unreadable chunk says nothing
        return None


def body_text(ctx: Any, event: Mapping[str, Any]) -> str:
    """The message's whole body (the store's text field), else its preview."""
    handle = _handle(ctx, event["object_id"])
    text = _store_text(ctx, handle, "row.body_text") if handle else None
    return text or event.get("text") or ""


class CaptureTexts:
    """Read-only: an attachment's captured text by its handle (the mail capture store's
    text blob), for the attachments whose text the projection does not hold."""

    def __init__(self, state: Path | str) -> None:
        self.state = Path(state)
        self.conn = sqlite3.connect(f"file:{self.state / 'mail-capture' / 'capture.db'}?mode=ro",
                                    uri=True, timeout=30, check_same_thread=False)
        self.dirs = [self.state / "mail-capture" / "blobs" / "sha256",
                     self.state / "mail-capture" / "blobs.nvme" / "sha256"]

    def text(self, handle: str) -> tuple[str | None, str | None]:
        from integrations.captured_text import read_blob
        row = self.conn.execute("SELECT json_extract(metadata, '$.name'), text_sha256 FROM "
                                "attachments WHERE l1_handle = ?", (handle,)).fetchone()
        if not row or not row[1]:
            return (row[0] if row else None), None
        for d in self.dirs:
            raw = read_blob(d, row[1])
            if raw is not None:
                return row[0], raw.decode("utf-8", "replace")
        return row[0], None

    def close(self) -> None:
        self.conn.close()


class MCQuestions:
    """Read-only: a question an MC agent asked a person on a task, read from the MC capture
    (``mc-capture/capture.sqlite``, the current version of the task event and of its task),
    for the question fields the projection does not hold. ``asked`` returns the question as
    it stood when asked (addressee, text, options, context, the asker's recommended option
    and reason) and, apart, when and by whom it was answered; the answer's own text is never
    returned (a replay of past days must not see it)."""

    KIND = "mission_control"

    def __init__(self, state: Path | str) -> None:
        path = Path(state) / "mc-capture" / "capture.sqlite"
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30,
                                    check_same_thread=False)
        self._home: frozenset[str] | None = None
        self._live_since: Any = None

    def home_domains(self) -> frozenset[str]:
        """The domains MC writes full addresses in (task event actors and addressees): the
        only place a bare MC name (``vince``) is looked up (``autopilot.handle_person``)."""
        if self._home is None:
            found = set()
            for (who,) in self.conn.execute(
                    "SELECT json_extract(payload, '$.row.actor') FROM versions WHERE kind = ? "
                    "AND path LIKE '%/task_events/%' AND superseded_at IS NULL UNION "
                    "SELECT json_extract(payload, '$.row.payload.addressee') FROM versions "
                    "WHERE kind = ? AND path LIKE '%/task_events/%' AND superseded_at IS NULL",
                    (self.KIND, self.KIND)):
                if isinstance(who, str) and re.fullmatch(r"[^@\s:]+@[a-z0-9.-]+", who.lower()):
                    found.add(who.lower().rsplit("@", 1)[1])
            self._home = frozenset(found)
        return self._home

    @staticmethod
    def key_of(native_key: str) -> tuple[str, str] | None:
        """(scope, event id) of an MCTaskEvent's native key, else None."""
        try:
            raw = json.loads(native_key)
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        scope, eid = raw.get("scope"), raw.get("event_id")
        if not isinstance(scope, str) or not isinstance(eid, str) or "~" not in eid or \
                "/" in scope or "/" in eid:
            return None
        return scope, eid

    def _row(self, path: str) -> dict[str, Any] | None:
        got = self.conn.execute("SELECT payload FROM versions WHERE kind = ? AND path = ? AND "
                                "superseded_at IS NULL", (self.KIND, path)).fetchone()
        if not got:
            return None
        try:
            row = json.loads(got[0]).get("row")
        except (TypeError, ValueError, AttributeError):
            return None
        return row if isinstance(row, dict) else None

    def event(self, scope: str, event_id: str) -> dict[str, Any] | None:
        """The task event's kind, actor and question id (asked or answered), else None.
        ``by_agent``: an agent runtime took the step under the actor's identity (the payload
        names its ``runtime`` or an ``actorKind`` other than a human's), not the person."""
        row = self._row(f"{scope}/task_events/{event_id}")
        if row is None:
            return None
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        kind_of_actor = str(payload.get("actorKind") or "").lower()
        return {"kind": str(row.get("kind") or ""), "actor": str(row.get("actor") or ""),
                "by_agent": bool(payload.get("runtime")) or kind_of_actor not in ("", "human"),
                "task_id": str(row.get("task_id") or ""),
                "question_id": str(payload.get("questionId") or ""),
                "addressee": str(payload.get("addressee") or "")}

    def asked(self, scope: str, event_id: str) -> dict[str, Any] | None:
        """The question a ``question.asked`` event names, else None (unreadable: no item)."""
        ev = self.event(scope, event_id)
        if ev is None or ev["kind"] != "question.asked" or not ev["question_id"]:
            return None
        task = self._row(f"{scope}/tasks/{ev['task_id']}") if ev["task_id"] else None
        q = next((q for q in (task or {}).get("questions") or ()
                  if isinstance(q, Mapping) and q.get("id") == ev["question_id"]), None)
        if q is None:
            return None
        options = [str(o.get("label") or o.get("text") or "") if isinstance(o, Mapping)
                   else str(o) for o in q.get("options") or ()][:8]
        rec = q.get("recommended")
        answer = q.get("answer") if isinstance(q.get("answer"), Mapping) else {}
        withdrawn = q.get("withdrawal") if isinstance(q.get("withdrawal"), Mapping) else {}
        return {"question_id": ev["question_id"], "task_id": ev["task_id"],
                "addressee": str(q.get("addressee") or ev["addressee"]).strip().lower(),
                "asked_at": str(q.get("askedAt") or ""),
                "text": str(q.get("text") or "")[:ap.TEXT_CHARS],
                "context": str(q.get("context") or "")[:1500],
                "options": options,
                "recommended": rec if isinstance(rec, int) and 0 <= rec < len(options) else None,
                "reason": str(q.get("recommendationReason") or "")[:800],
                "answered_at": str(answer.get("answeredAt") or ""),
                "answered_by": str(answer.get("answeredBy") or "").strip().lower(),
                "withdrawn_at": str(withdrawn.get("at") or withdrawn.get("withdrawnAt") or "")}

    # the capture units an MC approval or question and its answer are read from
    UNITS = ("approvals", "task_events")

    def known_to(self) -> Any:
        """Until when the capture holds MC approvals and task events: the earlier of the two
        units' last write (a run in progress writes as it reads). A unit never run: the epoch
        (nothing known, so no MC item is judged). None only for a capture without a runs
        table (an older layout): its end is unknown and the caller sets no bound."""
        try:
            got = [self.conn.execute(
                "SELECT max(updated_at) FROM runs WHERE kind = ? AND unit LIKE ?",
                (self.KIND, f"%/{unit}")).fetchone() for unit in self.UNITS]
        except sqlite3.OperationalError:
            return None
        ends = [ap._instant(g[0]) if g and g[0] else None for g in got]
        if any(e is None for e in ends):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        return min(ends)

    # an MC record type -> its folder in the capture (the native key names scope and id)
    CAPTURED_AS = {"MCApproval": ("approvals", "id"), "MCTaskEvent": ("task_events", "event_id")}

    def first_held(self, otype: str, native_key: str, happened: Any) -> Any:
        """When the live MC feed first held this record: its first captured version's time,
        only when the capture was already reading MC live when the record happened (a record
        from before was read by the first sweep's backfill, which says nothing of the feed's
        cadence). Else None, and the caller falls back to the configured feed lag. A capture
        without version times or runs (an older layout) is None too."""
        folder = self.CAPTURED_AS.get(otype)
        try:
            raw = json.loads(native_key)
        except (TypeError, ValueError):
            return None
        if folder is None or not isinstance(raw, Mapping) or happened is None:
            return None
        scope, rid = raw.get("scope"), raw.get(folder[1])
        if not isinstance(scope, str) or not isinstance(rid, str) or not scope or not rid \
                or "/" in scope or "/" in rid:
            return None
        try:
            if self._live_since is None:
                got = self.conn.execute("SELECT min(started_at) FROM runs WHERE kind = ?",
                                        (self.KIND,)).fetchone()
                self._live_since = ap._instant(got[0] if got else None) or False
            if not self._live_since or happened < self._live_since:
                return None
            got = self.conn.execute(
                "SELECT min(recorded_at) FROM versions WHERE kind = ? AND path = ?",
                (self.KIND, f"{scope}/{folder[0]}/{rid}")).fetchone()
        except sqlite3.OperationalError:
            return None
        held = ap._instant(got[0] if got else None)
        return held if held is not None and held >= happened else None

    def close(self) -> None:
        self.conn.close()


def attachments(ctx: Any, event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The message's attachments with text: (name, form, text, handle, record)."""
    from integrations import sheet_readings as sr
    out = []
    atts = [o for o in ctx.linked(event["object_id"], ("Attachment.attached_to",))
            if o.startswith("Attachment:")][:MAX_ATTACHMENTS * 2]
    for att in sorted(atts):
        handle = _handle(ctx, att)
        if not handle:
            continue
        name = _store_text(ctx, handle, "row.name") or ""
        text = _store_text(ctx, handle, "row.content_text")
        capture = getattr(ctx, "capture", None)
        if text is None and capture is not None:
            got_name, text = capture.text(handle)
            name = name or got_name or ""
        if not text:
            continue
        form = sr.form_of(name) or "text"
        ctx.objects.add(att)
        if handle not in ctx.handles:
            ctx.handles.append(handle)
        out.append({"name": name, "form": form, "text": text, "handle": handle,
                    "object_id": att})
        if len(out) >= MAX_ATTACHMENTS:
            break
    return out


# ----------------------------------------------------------------------------- parties

def _customer_rows(ctx: Any, oids: Sequence[str]) -> dict[str, dict[str, Any]]:
    return ctx.read(oids, ("name", "erp_code", "bill_to_erp_code", "code", "bc_blocked",
                           "deleted_at", "price_list_code", "email", "bc_email", "currency"))


def resolve_customer(ctx: Any, event: Mapping[str, Any], text: str) -> dict[str, Any]:
    """The ordering customer (module doc): ``{"number", "name", "records", "basis"}`` or
    ``{"number": None, "reason"}``. Conservative: two customer numbers left after folding
    twins, ship-tos of one bill-to, and the PO's own words are no customer."""
    sender = event.get("sender") or ""
    linked = ctx.linked(event["object_id"], ("Email.sent_by_customer",))
    basis = "mail_link"
    cands = [o for o in linked if o.startswith("Customer:")]
    if not cands:
        found = ap._parties(ctx.st, sender)
        cands = sorted(o for side, o in found if side == "customer")
        exact = ap._party_addresses(ctx.st).get(sender) or set()
        exact_c = sorted(o for side, o in exact if side == "customer")
        basis = "sender_address" if exact_c else "sender_domain"
        cands = exact_c or cands
    if not cands:
        return {"number": None, "reason": "no_customer_states_the_sender"}
    twins = [o for o in ctx.linked_many(cands, ("Customer.same_record_as",))
             if o.startswith("Customer:")]
    rows = _customer_rows(ctx, sorted(set(cands) | set(twins)))
    live = {o: r for o, r in rows.items()
            if ap._v(r, "erp_code") and not DONT_USE.search(ap._v(r, "name"))
            and not ap._v(r, "deleted_at") and ap._v(r, "bc_blocked").lower() not in
            ("all", "true", "1", "invoice", "ship")}
    if not live:
        return {"number": None, "reason": "customer_records_have_no_number"}
    by_number: dict[str, list[str]] = defaultdict(list)
    for o, r in live.items():
        by_number[ap._v(r, "erp_code").upper()].append(o)
    low = text.lower()
    if len(by_number) > 1:
        # the PO names its customer number, or exactly one candidate's name
        stated = [n for n in by_number if re.search(r"(?<![\w-])" + re.escape(n.lower())
                                                    + r"(?![\w-])", low)]
        named = [n for n, os in by_number.items()
                 if any(len(ap._v(live[o], "name")) >= 6 and
                        ap._v(live[o], "name").lower() in low for o in os)]
        pick = stated if len(stated) == 1 else named if len(named) == 1 else []
        if pick:
            by_number = {pick[0]: by_number[pick[0]]}
            basis += "+named_in_po"
        else:
            bills = {ap._v(live[o], "bill_to_erp_code").upper() for os in by_number.values()
                     for o in os}
            if len(bills) == 1 and "" not in bills and next(iter(bills)) in by_number:
                bill = next(iter(bills))
                by_number = {bill: by_number[bill]}
                basis += "+bill_to"
    if len(by_number) != 1:
        return {"number": None, "reason": "several_customers", "candidates":
                sorted(by_number)[:6]}
    number, records = next(iter(by_number.items()))
    row = live[records[0]]
    ctx.cite(row, "erp_code", "name")
    return {"number": number, "name": ap._v(row, "name"), "records": sorted(records),
            "basis": basis, "currency": ap._v(row, "currency") or None,
            "price_list": ap._v(row, "price_list_code") or None}


def resolve_items(ctx: Any, tokens: Sequence[str]) -> dict[str, list[str]]:
    """token (upper) -> the item records whose item number is exactly it."""
    wanted = sorted({t.upper() for t in tokens} | {t.upper().replace(" ", "") for t in tokens})
    if not wanted:
        return {}
    ctx.check()
    rows = ctx.st.conn.execute(
        "SELECT normalized, object_id FROM identifiers WHERE normalized IN (SELECT value FROM "
        "json_each(?)) AND identifier_kind IN ('item_code', 'erp_item_code') AND "
        "superseded_at IS NULL", (json.dumps(wanted),)).fetchall()
    out: dict[str, list[str]] = defaultdict(list)
    for norm, oid in rows:
        if oid.startswith("Product:") and oid not in out[norm.upper()]:
            out[norm.upper()].append(oid)
    return dict(out)


def line_item(found: Mapping[str, list[str]], tokens: Sequence[str]) -> tuple[str, list[str]] | None:
    """The one item number a line's tokens name (twins and aliases of one number are one
    item); none or several: None."""
    hits = {t.upper(): found[t.upper()] for t in tokens if found.get(t.upper())}
    if len(hits) == 1:
        return next(iter(hits.items()))
    if len(hits) > 1:
        records = [set(v) for v in hits.values()]
        if all(r & records[0] for r in records[1:]):
            first = next(iter(hits))
            return first, sorted(set().union(*records))
    return None


# ----------------------------------------------------------------------------- prices, ATP

def last_prices(ctx: Any, customer_records: Sequence[str]) -> dict[str, dict[str, Any]]:
    """item number -> the customer's last invoiced (else ordered) unit price, with its
    date, document and unit (Decimal text); read from its latest documents only."""
    out: dict[str, dict[str, Any]] = {}
    for head_link, line_link, date_prop, qty_prop, label in (
            ("SalesInvoice.billed_to", "SalesInvoiceLine.line_of", "invoice_date",
             "invoiced_quantity", "invoice"),
            ("SalesOrder.ordered_by", "SalesOrderLine.line_of", "order_date",
             "ordered_quantity", "order")):
        heads = [o for o in ctx.linked_many(customer_records, (head_link,))
                 if not o.startswith("Customer:")]
        rows = ctx.read(heads[:3000], (date_prop, "invoice_number", "order_number", "currency"))
        latest = sorted(rows, key=lambda o: ap._v(rows[o], date_prop), reverse=True)
        latest = latest[:PRICE_INVOICES]
        lines_of = ctx.touching(latest, (line_link,))
        line_ids = sorted({a for _t, a, _b in lines_of})
        head_of = {a: b for _t, a, b in lines_of}
        lrows = ctx.read(line_ids, ("unit_price", qty_prop, "uom", "sage_item_code"))
        items = ctx.touching(line_ids, (line_link.split(".")[0] + ".for_item",))
        item_of = {a: b for _t, a, b in items}
        codes = ctx.read(sorted(set(item_of.values())), ("code",))
        for line, r in lrows.items():
            price = ap._decimal(ap._v(r, "unit_price"))
            head = head_of.get(line)
            code = (ap._v(codes.get(item_of.get(line, "")), "code") or
                    ap._v(r, "sage_item_code")).upper().strip()
            if price is None or price <= 0 or not code or head not in rows:
                continue
            day = ap._v(rows[head], date_prop)[:10]
            if code not in out or day > out[code]["date"]:
                out[code] = {"price": str(price), "date": day, "basis": f"last {label}",
                             "document": ap._v(rows[head], "invoice_number") or
                             ap._v(rows[head], "order_number"),
                             # the price's own currency and the unit it is per (its
                             # cell), else its document's currency and the line's unit
                             "currency": (r.get("unit_price") or {}).get("currency") or
                             ap._v(rows[head], "currency") or None,
                             "uom": ap._qty_unit(r, "unit_price", qty_prop) or None,
                             "object_id": head}
        if out:
            break
    return out


def item_available(ctx: Any, items: Sequence[str], need: Decimal,
                   unit: str | None = None) -> dict[str, Any]:
    """The day ``need`` (in ``unit``: the whole order's need of the item, its lines summed
    first) is available: on hand when a record states it, else open PO lines by expected
    date, each line read with its order's header (a closed or cancelled PO supplies
    nothing: ``ap.supply_flows``); ``None`` with the reason when the data does not say,
    or when the stock, the PO lines and the need are not in one stated unit."""
    from ontology.units import unit_token
    rows = ctx.read(items, ("quantity_on_hand",))
    stated = [(d, ap._qty_unit(r, "quantity_on_hand")) for r in rows.values()
              if (d := ap._decimal(ap._v(r, "quantity_on_hand"))) is not None]
    if not stated:
        return {"date": None, "basis": "not checked: no on-hand figure in the records"}
    on_hand, stock_unit = max(stated, key=lambda s: s[0])
    unit = unit_token(unit) if unit else None
    if unit is None or stock_unit != unit:
        return {"date": None, "basis": "not checked: the order's unit "
                f"({unit or 'not stated'}) and the stock's ({stock_unit or 'not stated'}) "
                "are not one stated unit; units are never converted"}
    if on_hand >= need:
        return {"date": ctx.as_of.isoformat(), "basis": f"on hand {on_hand}"}
    supply = on_hand
    po_lines = ctx.touching(items, ("PurchaseOrderLine.for_item",))
    flows, why = ap.supply_flows(ctx, [a for _t, a, _b in po_lines][:200], unit)
    if why is not None:
        return {"date": None, "basis": f"not checked: an open PO line's unit is "
                f"{'not stated' if why == 'unit_unknown' else 'another unit'}"}
    for at, due, _pl in flows:
        supply += due
        if supply >= need:
            return {"date": at.isoformat(), "basis": "on hand, then open PO lines"}
    return {"date": None, "basis": "not checked: stock and open PO lines do not cover it"}


# ----------------------------------------------------------------------------- drafts

def first_name(address: str, display: str | None = None) -> str | None:
    """A person's first name from their display name, else an address of the shape
    ``first.last@`` (``jdoe@`` names no first name: no greeting)."""
    local = address.split("@", 1)[0]
    split = re.split(r"[._]", local)
    for raw in ((display or "").split(" ")[0], split[0] if len(split) > 1 else ""):
        if FIRST_NAME.match(raw.capitalize()) and raw.lower() not in (
                "info", "sales", "orders", "order", "billing", "admin", "accounts", "office",
                "purchasing", "support", "service", "team", "hello", "contact", "ap", "ar",
                "dropship", "customerservice", "noreply", "operations", "logistics"):
            return raw.capitalize()
    return None


def draft(to: str, subject: str, parts: Sequence[str], facts: Mapping[str, Any], *,
          in_reply_to: str | None = None, display: str | None = None,
          today: date | None = None) -> dict[str, Any] | None:
    """A drafted message in the company voice through the copy gate; None when its first
    sentence does not pass (no draft beats a wrong one)."""
    from . import copy_gate as cg
    name = first_name(to, display)
    lead = [f"Hi {name},"] if name else []
    fact_list = cg.facts_of(facts) + ([("first_name", name)] if name else [])
    body, check = cg.settle([*lead, *parts], surface="draft_body", facts=fact_list,
                            required=len(lead) + 1, joiner="\n", today=today)
    if body is None:
        return None
    if lead:
        body = body.replace(lead[0] + "\n", lead[0] + "\n\n", 1)
    if subject and not re.match(r"^\s*re\s*:", subject, re.I) and in_reply_to:
        subject = "RE: " + subject
    return {"channel": "email", "to": [{"address": to}], "subject": subject[:200],
            "body": body, "in_reply_to": in_reply_to, "copy_check": {
                "ok": check.get("ok"), "findings": len(check.get("findings") or ())}}


def say(day: str | None, today: date) -> str:
    from . import copy_gate as cg
    return cg.say_date(day, today) if day else "not stated"


def _qty(value: str) -> str:
    d = ap._decimal(value)
    return format(d.normalize(), "f") if d is not None else value


# ----------------------------------------------------------------------------- customer PO

def h_order_entry(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    subject = event.get("subject") or ""
    body = body_text(ctx, event)
    files = attachments(ctx, event)
    pos = dl.unique([*dl.po_numbers(subject), *ap._numbers(ap.PO_NUMBER, subject),
                     *[p for f in files for p in dl.po_numbers(f["text"])],
                     *dl.po_numbers(body[:2000])])
    if not pos and re.match(r"^\s*(?:re|aw|sv|fw|fwd)\s*:", subject, re.I):
        return {"skip": "reply_without_po_number"}
    named = ap.identified(ctx, subject, body[:2000])
    if named and all(o.startswith(("PurchaseOrder:", "Receipt:")) for o in named):
        return {"skip": "names_our_purchase_order"}
    entered = [o for o in named if o.startswith("SalesOrder:")]
    for po in pos:
        entered += [o for o in ctx.find("SalesOrder", "customer_po", po)]
    if entered:
        f = ap._order_facts(ctx, entered[0])
        atp = ap.availability(ctx, entered[0])
        facts = {"po": pos[0] if pos else f["customer_po"], "order": f["order_number"],
                 "status": f["status"], "ship": atp["date"]}
        msg = draft(event.get("sender") or "", subject, [
            f"Thanks, PO {facts['po']} is entered as our order {f['order_number']}.",
            f"It can ship {say(atp['date'], ctx.as_of)}." if atp["date"] else
            "We'll confirm the ship date shortly."],
            facts, in_reply_to=event["object_id"], today=ctx.as_of)
        return {"summary": f"PO {facts['po']} is already entered as order "
                           f"{f['order_number']}.", "order": f, "draft": msg,
                "availability": atp,
                "links": [{"label": "Open the order", "href": f"/object/{entered[0]}"}],
                "done_hint": "order_entered", "gaps": []}
    sources = [(f["name"], f["form"], f["text"], f["handle"]) for f in files] + \
        [("mail body", "text", body, None)]
    lines, read_from = [], None
    for name, form, text, handle in sources:
        got = dl.read_lines(text, form)
        if got:
            lines, read_from = got, {"name": name, "handle": handle}
            break
    po_text = "\n".join(t for _n, _f, t, _h in sources)
    customer = resolve_customer(ctx, event, po_text[:20000])
    found = resolve_items(ctx, [t for ln in lines for t in ln["tokens"]])
    for ln in lines:                    # an item code the identifier table lacks: its record
        if not any(found.get(t.upper()) for t in ln["tokens"]):
            for t in ln["tokens"][:3]:
                hits = ctx.find("Product", "code", t)
                if hits:
                    found[t.upper()] = hits
    prices = last_prices(ctx, customer["records"]) if customer.get("number") else {}
    from ontology.units import currency_code, unit_token
    want_currency = currency_code(customer.get("currency") or "")[0]
    out_lines, unresolved, priced_out = [], [], 0
    resolved = []
    for ln in lines:
        hit = line_item(found, ln["tokens"])
        if hit is None:
            unresolved.append({"text": ln["text"], "cite": ln["cite"],
                               "reason": "no item number, or several, match the line"})
            continue
        resolved.append((ln, hit))
    # the order's whole need of each item (its lines summed first, in one unit): two lines
    # of one item never each count the same stock (AUTOMATION-003)
    need_of: dict[str, tuple[Decimal, set]] = {}
    for ln, (code, _records) in resolved:
        total, units = need_of.get(code, (Decimal(0), set()))
        need_of[code] = (total + (ap._decimal(ln["quantity"]) or Decimal(0)),
                         units | {unit_token(ln.get("unit") or "")})
    checked: dict[str, dict[str, Any]] = {}
    for ln, (code, records) in resolved:
        last = prices.get(code)
        stated = ln.get("unit_price")
        # a last price is reused only in the line's unit and the customer's currency
        # (AUTOMATION-007): a USD price per case is no CAD price per kg
        reusable = bool(last and want_currency and
                        currency_code(last.get("currency") or "")[0] == want_currency and
                        unit_token(last.get("uom") or "") is not None and
                        unit_token(last.get("uom") or "") == unit_token(ln.get("unit") or ""))
        price = stated or ((last or {}).get("price") if reusable else None)
        if not stated and last and not reusable:
            # a last price we hold but may not reuse: never left for BC to fill silently
            priced_out += 1
        if code not in checked:
            total, units = need_of[code]
            checked[code] = item_available(ctx, records, total,
                                           next(iter(units)) if len(units) == 1 else None)
        avail = checked[code]
        out_lines.append({"item": code, "quantity": ln["quantity"], "unit": ln.get("unit"),
                          "po_unit_price": stated, "last_price": last,
                          "unit_price": price,
                          "price_basis": "the PO's price" if stated else
                          ((last or {}).get("basis") if reusable else
                           "not found" if not last else
                           "the last price is in another unit or currency"),
                          "price_differs": bool(stated and last and ap._decimal(stated) !=
                                                ap._decimal(last["price"])),
                          "available": avail, "cite": ln["cite"], "items": records[:4]})
    out: dict[str, Any] = {"po_numbers": pos, "customer": customer, "lines": out_lines,
                           "unresolved_lines": unresolved, "read_from": read_from,
                           "gaps": [], "links": []}
    if not lines:
        out["gaps"].append("no_lines_read")
    if unresolved:
        out["gaps"].append("lines_not_resolved")
    if not customer.get("number"):
        out["gaps"].append("customer_not_resolved:" + customer.get("reason", ""))
    if not pos:
        out["gaps"].append("po_number_not_found")
    if priced_out:
        out["gaps"].append("price_unresolved")
    if not out_lines and not customer.get("number"):
        return {"skip": "nothing_resolved"}     # no customer, no line: nothing to prepare
    dates = [ln["available"]["date"] for ln in out_lines]
    ship = max(dates) if out_lines and all(dates) else None
    ready = bool(customer.get("number") and pos and out_lines and not unresolved
                 and not priced_out)
    if ready:
        out["write"] = {
            "system": "bc", "operation": "sales_order_create",
            "payload": {"customerNumber": customer["number"],
                        "externalDocumentNumber": pos[0][:35],
                        "orderDate": ctx.as_of.isoformat(),
                        "salesOrderLines": [
                            {"lineType": "Item", "lineObjectNumber": ln["item"],
                             "quantity": ln["quantity"],
                             **({"unitPrice": ln["unit_price"]} if ln["unit_price"] else {}),
                             **({"unitOfMeasureCode": ln["unit"]} if ln["unit"] else {})}
                            for ln in out_lines]},
            "reason": f"Customer PO {pos[0]} received by mail; {len(out_lines)} lines read "
                      f"from {(read_from or {}).get('name') or 'the message'}.",
            "precondition": {"party": customer["number"]}}
    n = len(out_lines)
    out["summary"] = (f"New PO {pos[0] if pos else '(no number)'}"
                      + (f" from {customer['name']} ({customer['number']})"
                         if customer.get("number") else "")
                      + f": {n} of {len(lines)} lines resolved"
                      + ("; order entry ready to review" if ready else "")
                      + (f"; ships {ship}" if ship else "; ship date not checked") + ".")
    if ready:
        facts = {"po": pos[0], "lines": n, "ship": ship}
        parts = [f"Thanks, we have PO {pos[0]} with {n} line{'s' if n != 1 else ''}."]
        parts.append(f"It can ship {say(ship, ctx.as_of)}." if ship else
                     "We'll confirm the ship date shortly.")
        out["draft"] = draft(event.get("sender") or "", subject, parts,
                             {**facts, "ship_day": say(ship, ctx.as_of) if ship else None},
                             in_reply_to=event["object_id"], today=ctx.as_of)
    return out


# ----------------------------------------------------------------------------- supplier POs

PO_HEAD = ("po_number", "status", "bc_status", "order_date", "expected_date", "vendor_name",
           "bc_fully_received", "sage_is_complete", "currency", "total_cost")


def _po_open(row: Mapping[str, Any]) -> bool:
    status = " ".join(ap._v(row, p) for p in ("status", "bc_status")).lower()
    if ap.CLOSED.search(status) or "received" in status or "draft" in status:
        return False
    return ap._v(row, "bc_fully_received").lower() not in ("true", "1") and \
        ap._v(row, "sage_is_complete").lower() not in ("true", "1", "yes")


def po_due_events(st: Any, registry: Mapping[str, Any], as_of: date, domains: Sequence[str],
                  directory: Mapping[str, str], addresses_of: Mapping[str, set[str]]
                  ) -> list[dict[str, Any]]:
    """``record.due`` events: open POs ordered between ``window_days`` and ``after_days``
    ago with no supplier confirmation, receipt or supplier reply (one event per PO)."""
    spec = next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == "po_followup" and a.get("enabled") is not False),
                None)
    if spec is None:
        return []
    after = int((spec.get("params") or {}).get("after_days", 2))
    window = int((spec.get("params") or {}).get("window_days", 21))
    since = (as_of - timedelta(days=window)).isoformat()
    until = (as_of - timedelta(days=after)).isoformat() + "T23:59:59Z"
    ids = sorted({r[0] for r in st.conn.execute(
        # events first (``autopilot.window_ids``: the store has no time index)
        "SELECT DISTINCT eo.object_id FROM events e CROSS JOIN event_objects eo ON "
        "eo.event_id = e.event_id WHERE e.event_type IN ('PurchaseOrder.created', "
        "'PurchaseOrder.ordered', 'PurchaseOrder.sage_ordered') AND e.time >= ? AND e.time <= ?", (since, until))
        if r[0].startswith("PurchaseOrder:")})
    rows = ap._read(st, ids, PO_HEAD)
    out = []
    seen_numbers: set[str] = set()
    for oid in ids:
        row = rows.get(oid) or {}
        number = ap._v(row, "po_number")
        if not number or not _po_open(row) or number.upper() in seen_numbers:
            continue
        links = ap._linked(st, oid)
        kinds = {t for t, _o in links}
        if kinds & {"SupplierConfirmation.confirms", "Receipt.receives",
                    "PurchaseOrder.bc_posted_as"}:
            continue
        twins = [o for t, o in links if t == "PurchaseOrder.same_record_as"][:4]
        twin_kinds = {t for row in st.touching(twins, ("SupplierConfirmation.confirms",
                                                       "Receipt.receives"))
                      for t in (row[2],)} if twins else set()
        if twin_kinds:
            continue
        links += [(row[2], row[3] if row[4] in twins else row[4])
                  for row in (st.touching(twins, None) if twins else [])]
        mails = [o for _t, o in links if o.startswith("Email:")]
        mrows = ap._read(st, mails[:40], ("sender", "to_addresses", "sent_at", "mailbox"))
        replied, owner, to = False, None, None
        for m, r in sorted(mrows.items(), key=lambda x: ap._v(x[1], "sent_at")):
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            if not sender:
                continue
            if ap._internal(sender, domains):
                ext = [a for a in ap._addresses(ap._v(r, "to_addresses"))
                       if not ap._internal(a, domains)]
                if ext and directory.get(sender):
                    owner, to = directory[sender], ext[0]
            else:
                replied = True
        if replied:
            continue
        if owner is None:             # no outbound copy: the person whose mailbox holds it
            boxes = sorted({ap._v(r, "mailbox").lower() for r in mrows.values()
                            if ap._v(r, "mailbox").lower() in addresses_of})
            owner = boxes[0] if len(boxes) == 1 else None
        seen_numbers.add(number.upper())
        out.append({"event_id": ap._digest("record.due", "po_followup", oid),
                    "event_type": "record.due", "object_id": oid,
                    "object_type": "PurchaseOrder", "at": ap._v(row, "order_date"),
                    "status": ap._v(row, "status"), "owner_hint": owner, "supplier_to": to,
                    "audience": ap._types(st, [oid]).get(oid, ("", ""))[1],
                    "handles": [c["l1_handle"] for c in row.values()
                                if c.get("l1_handle")][:3],
                    "fields": {"po_number": number,
                               "order_date": ap._v(row, "order_date")[:10]}})
    return out


def _po_lines(ctx: Any, po: str) -> list[dict[str, Any]]:
    lines = ctx.linked(po, (ap.PO_LINE_OF,))[:ap.MAX_LINES]
    rows = ctx.read(lines, ("item_code", "description", "ordered_quantity", "received_quantity",
                            "uom", "bc_expected_receipt_date", "line_type"))
    out = []
    for line in sorted(rows):
        r = rows[line]
        qty = ap._decimal(ap._v(r, "ordered_quantity"))
        if qty is None or qty <= 0:
            continue
        ctx.cite(r, "item_code", "ordered_quantity")
        out.append({"line": line, "item": ap._v(r, "item_code") or ap._v(r, "description")[:40],
                    "quantity": _qty(ap._v(r, "ordered_quantity")), "uom": ap._v(r, "uom"),
                    "expected": ap._v(r, "bc_expected_receipt_date")[:10] or None})
    return out


def _supplier_contact(ctx: Any, po: str) -> str | None:
    sups = ctx.linked(po, ("PurchaseOrder.ordered_from",))
    rows = ctx.read(sups, ("csr_email", "rep_email", "bc_email"))
    found = []
    for r in rows.values():
        for p in ("csr_email", "rep_email", "bc_email"):
            for a in ap._addresses(ap._v(r, p)):
                if a not in found:
                    found.append(a)
                    ctx.cite(r, p)
    return found[0] if len(set(found)) >= 1 else None


def h_po_followup(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    po = event["object_id"]
    head = ctx.read([po], PO_HEAD).get(po) or {}
    if not _po_open(head):
        return {"skip": "po_closed"}
    ctx.cite(head, "po_number", "order_date")
    lines = _po_lines(ctx, po)
    if not lines:
        return {"skip": "po_has_no_lines"}
    to = event.get("supplier_to") or _supplier_contact(ctx, po)
    if not to:
        return {"skip": "supplier_contact_unknown"}
    number, ordered = ap._v(head, "po_number"), ap._v(head, "order_date")[:10]
    age = (ctx.as_of - date.fromisoformat(ordered)).days if ordered else None
    shown = lines[:3]
    listed = "; ".join(f"{x['item']} x {x['quantity']}" + (f" {x['uom']}" if x["uom"] else "")
                       for x in shown)
    more = len(lines) - len(shown)
    facts = {"po": number, "ordered": ordered, "lines": len(lines), "more": more or None,
             **{f"item{i}": x["item"] for i, x in enumerate(shown)},
             **{f"qty{i}": x["quantity"] for i, x in enumerate(shown)},
             "ordered_day": say(ordered, ctx.as_of)}
    parts = [f"Could you confirm PO {number} from {say(ordered, ctx.as_of)} and send the "
             f"ship date?", f"Lines: {listed}" + (f" and {more} more." if more else ".")]
    msg = draft(to, f"PO {number}: confirmation and ship date", parts, facts, today=ctx.as_of)
    if msg is None:
        return {"skip": "draft_failed_copy_gate"}
    return {"summary": f"PO {number} to {ap._v(head, 'vendor_name') or 'the supplier'} has no "
                       f"confirmation or ETA after {age} days; follow-up drafted.",
            "purchase_order": {"object_id": po, "number": number, "ordered": ordered,
                               "expected": ap._v(head, "expected_date")[:10] or None,
                               "supplier": ap._v(head, "vendor_name"), "lines": lines},
            "draft": msg, "gaps": [],
            "links": [{"label": "Open the PO", "href": f"/object/{po}"}]}


_MONTH = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"), 1)}
ETA_WORDS = re.compile(r"\b(?:eta|ship(?:s|ping|ped)?(?: date)?|deliver(?:y|ed|s)?|arriv\w*|"
                       r"ready|dispatch\w*|in stock|available|lead time|pick ?up)\b", re.I)
DATE_TEXT = re.compile(
    r"\b(?:(?P<iso>20\d{2}-\d{2}-\d{2})|(?P<m1>\d{1,2})/(?P<d1>\d{1,2})(?:/(?P<y1>\d{2,4}))?|"
    r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d2>\d{1,2})"
    r"(?:st|nd|rd|th)?(?:,?\s+(?P<y2>20\d{2}))?|(?P<d3>\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(?P<mon3>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+(?P<y3>20\d{2}))?)",
    re.I)


def stated_dates(text: str, as_of: date) -> list[date]:
    """Dates stated within a few words after an ETA, ship or delivery word (a year left out
    is the next such day on or after 60 days before ``as_of``)."""
    out: list[date] = []
    for w in ETA_WORDS.finditer(text or ""):
        window = text[w.end():w.end() + 60]
        m = DATE_TEXT.search(window)
        if not m:
            continue
        try:
            if m["iso"]:
                day = date.fromisoformat(m["iso"])
            else:
                mon = int(m["m1"]) if m["m1"] else _MONTH[(m["mon"] or m["mon3"])[:3].lower()]
                d = int(m["d1"] or m["d2"] or m["d3"])
                y = m["y1"] or m["y2"] or m["y3"]
                year = (int(y) + (2000 if len(y) == 2 else 0)) if y else as_of.year
                day = date(year, mon, d)
                if not y and day < as_of - timedelta(days=60):
                    day = date(year + 1, mon, d)
        except (ValueError, KeyError):
            continue
        if as_of - timedelta(days=60) <= day <= as_of + timedelta(days=365) and day not in out:
            out.append(day)
    return out


def _impacted(ctx: Any, po: str, by: date) -> list[dict[str, Any]]:
    """Open customer orders that use the PO's items and are required before ``by``
    (pegging links first when the store holds them)."""
    pegged = [o for o in ctx.linked(po) if o.startswith("SalesOrder:")]
    orders: dict[str, str] = {o: "" for o in pegged}
    for pl in ctx.linked(po, (ap.PO_LINE_OF,))[:ap.MAX_LINES]:
        for item in ctx.linked(pl, (ap.PO_LINE_ITEM,))[:1]:
            code = ap._v(ctx.read([item], ("code",)).get(item), "code")
            for sl in ctx.linked(item, (ap.SO_LINE_ITEM,))[:200]:
                for order in ctx.linked(sl, (ap.SO_LINE_OF,))[:1]:
                    orders.setdefault(order, code)
    rows = ctx.read(list(orders)[:300], ("order_number", "status", "required_date"))
    out = []
    for o, r in rows.items():
        req = ap._instant(ap._v(r, "required_date"))
        if ap.CLOSED.search(ap._v(r, "status")) or req is None or req.date() < ctx.as_of:
            continue
        if req.date() <= by:
            ctx.cite(r, "order_number", "required_date")
            out.append({"object_id": o, "order_number": ap._v(r, "order_number"),
                        "required_date": ap._v(r, "required_date")[:10], "item": orders[o],
                        "pegged": o in pegged})
    return sorted(out, key=lambda x: x["required_date"])[:20]


def h_supplier_eta(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    subject = event.get("subject") or ""
    text = body_text(ctx, event)
    own = text.split("\nFrom:")[0][:4000]
    pos = [o for o in event.get("records") or () if o.startswith("PurchaseOrder:")]
    pos += [o for o in ap.identified(ctx, subject, own) if o.startswith("PurchaseOrder:")]
    for no in ap._numbers(ap.PO_NUMBER, subject, own):
        pos += ctx.find("PurchaseOrder", "po_number", no)
    pos = list(dict.fromkeys(pos))
    via_items: dict[str, list[str]] = {}
    if not pos:                 # the supplier's own item numbers -> our item -> its open PO
        from . import item_xref
        pos, via_items = item_xref.po_from_vendor_numbers(ctx, event, subject, own)
    if len(pos) != 1:
        return {"skip": "no_single_purchase_order_named" if not pos else "several_pos_named"}
    days = stated_dates(own, ctx.as_of)
    if len(days) != 1:
        return {"skip": "no_single_date_stated" if not days else "several_dates_stated"}
    po = pos[0]
    head = ctx.read([po], PO_HEAD).get(po) or {}
    ctx.cite(head, "po_number", "expected_date")
    new = days[0]
    old = ap._instant(ap._v(head, "expected_date"))
    old_d = old.date() if old else None
    slip = (new - old_d).days if old_d else None
    out: dict[str, Any] = {
        "purchase_order": {"object_id": po, "number": ap._v(head, "po_number"),
                           "supplier": ap._v(head, "vendor_name"),
                           "expected": old_d.isoformat() if old_d else None},
        "eta": {"stated": new.isoformat(), "previous": old_d.isoformat() if old_d else None,
                "slip_days": slip, "source": event["object_id"]},
        "gaps": [] if old_d else ["po_had_no_expected_date"],
        "links": [{"label": "Open the PO", "href": f"/object/{po}"}]}
    if via_items:
        out["matched_by_supplier_item"] = [{"supplier_number": tok, "items": items}
                                           for tok, items in sorted(via_items.items())]
    number = ap._v(head, "po_number")
    if slip is not None and slip > 0:
        out["impacted"] = _impacted(ctx, po, new)
        out["summary"] = (f"PO {number} ETA moves {slip} days to {new.isoformat()}; "
                          f"{len(out['impacted'])} open customer orders need it sooner.")
    elif slip is not None and slip <= 0:
        out["summary"] = f"PO {number} confirmed for {new.isoformat()} (on or before the " \
                         f"expected {old_d.isoformat()})."
    else:
        out["summary"] = f"PO {number}: supplier states {new.isoformat()}; the PO had no " \
                         "expected date. Record it."
    return out


# ----------------------------------------------------------------------------- owner's desk

FINANCE = re.compile(r"\b(?:bank|loan|line of credit|insurance|lease|capex|budget|invoice "
                     r"approval|wire|payment terms|credit limit|cash)\b", re.I)
PRICE_ASK = re.compile(r"\b(?:price|pricing|quote|discount|margin|cost increase|rate)\b", re.I)
ESCALATION = re.compile(r"\b(?:complain\w*|unacceptable|disappointed|escalat\w*|frustrat\w*|"
                        r"urgent|damaged|wrong (?:item|product)|missing)\b", re.I)
AMOUNT = re.compile(r"(?:\$|USD|CAD)\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")


def _status_lines(ctx: Any, orders: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
    lines, facts = [], {}
    for i, order in enumerate(orders[:3]):
        f = ap._order_facts(ctx, order)
        ship = f["shipments"][0] if f["shipments"] else None
        facts[f"order{i}"] = f["order_number"]
        if ship and ship["ship_date"]:
            facts[f"ship{i}"] = ship["ship_date"]
            facts[f"track{i}"] = ship["tracking"] or None
            lines.append(f"Order {f['order_number']} shipped {say(ship['ship_date'], ctx.as_of)}"
                         + (f" via {ship['carrier']}" if ship["carrier"] else "")
                         + (f", tracking {ship['tracking']}." if ship["tracking"] else "."))
        elif f["required_date"]:
            facts[f"req{i}"] = f["required_date"]
            lines.append(f"Order {f['order_number']} is {f['status'] or 'open'}, due "
                         f"{say(f['required_date'], ctx.as_of)}.")
        if f["status"]:
            facts[f"status{i}"] = f["status"]
    return lines, facts


def _current(ctx: Any, orders: Sequence[str], days: int = 120) -> list[str]:
    """The orders a message can be about now: ordered within ``days``, or stated open
    whatever their age (a dated order still open is current); an old order closed, or of
    no stated status, sharing a number with the ask is no answer. An undated order counts
    unless it is stated closed."""
    rows = ctx.read(orders, ("order_date", "status", "portal_created_at"))
    floor = (ctx.as_of - timedelta(days=days)).isoformat()
    out = []
    for o in orders:
        r = rows.get(o) or {}
        day = (ap._v(r, "order_date") or ap._v(r, "portal_created_at"))[:10]
        status = ap._v(r, "status")
        if (day and day >= floor) or (not ap.CLOSED.search(status)
                                      and (bool(status.strip()) or not day)):
            out.append(o)
    return out


def _po_currency(row: Mapping[str, Any] | None) -> str | None:
    """A PO header's currency: its total's own currency, else its currency cell."""
    from ontology.units import currency_code
    cell = (row or {}).get("total_cost")
    raw = (cell or {}).get("currency") if isinstance(cell, Mapping) else None
    return currency_code(raw or ap._v(row, "currency") or "")[0]


def h_desk(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    from . import proactive_threads as pt
    subject = event.get("subject") or ""
    text = body_text(ctx, event)
    own = text.split("\nFrom:")[0][:4000]
    intents = set(event.get("intents") or ())
    named = ap._records_named(ctx, {"subject": subject, "text": own,
                                    "records": event.get("records") or []})
    orders = [o for o in named if o.startswith("SalesOrder:")]
    pos = [o for o in named if o.startswith("PurchaseOrder:")]
    pos += [o for o in ap.identified(ctx, subject, own) if o.startswith("PurchaseOrder:")]
    pos = list(dict.fromkeys(pos))[:3]
    amounts = [a for a in AMOUNT.findall(own)][:3]
    out: dict[str, Any] = {"gaps": [], "evidence": [], "links": []}
    if "approval_request" in intents or pt.approval_ask(own):
        family = pt.approval_family(own)
        evidence, reasons, verdicts = [], [], []
        for po in pos:
            head = ctx.read([po], PO_HEAD).get(po) or {}
            ctx.cite(head, "po_number", "total_cost")
            sup = ctx.linked(po, ("PurchaseOrder.ordered_from",))
            prior = [o for o in ctx.linked_many(sup, ("PurchaseOrder.ordered_from",))
                     if o.startswith("PurchaseOrder:") and o != po]
            prow = ctx.read(prior[:200], ("total_cost", "order_date", "status", "currency"))
            this = ap._decimal(ap._v(head, "total_cost"))
            money = _po_currency(head)
            # the supplier's earlier POs in this PO's own currency only: a USD median is no
            # yardstick for a CAD total (amounts are never converted)
            totals = sorted(t for r in prow.values()
                            if (t := ap._decimal(ap._v(r, "total_cost"))) and t > 0
                            and money is not None and _po_currency(r) == money)
            evidence.append({"object_id": po, "po": ap._v(head, "po_number"),
                             "total": str(this) if this is not None else None,
                             "currency": money,
                             "supplier": ap._v(head, "vendor_name"),
                             "prior_pos": len(totals),
                             "median_prior": str(totals[len(totals) // 2]) if totals else None})
            number = ap._v(head, "po_number")
            if this is not None and totals:
                med = totals[len(totals) // 2]
                if this <= med * Decimal("1.25"):
                    verdicts.append(True)
                    reasons.append(f"PO {number} total {this} {money} is within 25% of this "
                                   f"supplier's median PO ({med} {money}, {len(totals)} POs)")
                else:
                    verdicts.append(False)
                    reasons.append(f"PO {number} total {this} {money} is above this "
                                   f"supplier's median PO ({med} {money})")
            else:
                verdicts.append(None)
                reasons.append(f"PO {number or po}: its total, currency or earlier POs in "
                               "that currency are not recorded, so it is not compared")
        # one recommendation for the whole ask: approve only when every PO named was
        # compared and each is within (AUTOMATION-011); any other mix is a review
        rec = "approve" if verdicts and all(v is True for v in verdicts) else "review"
        if not evidence:
            reasons.append("no PO or order named: decide from the message")
        out.update(kind="decision", family=family, recommendation=rec, reasons=reasons,
                   evidence=evidence, amounts=amounts,
                   summary=f"Decision ({family}): {subject[:100]} - recommend {rec}")
        return out
    if FINANCE.search(own) and (amounts or "?" in own) and \
            event.get("side") in ("internal", "customer", "supplier"):
        out.update(kind="decision", family="finance", recommendation="ask_cfo",
                   reasons=["cash position and AP/AR due are not in the records yet"],
                   amounts=amounts, gaps=["cash_position_not_held"],
                   summary=f"Finance ask: {subject[:100]}"
                           + (f" ({', '.join('$' + a for a in amounts)})" if amounts else ""))
        return out
    side = event.get("side")
    if ESCALATION.search(own) and side == "customer":
        cust = ctx.linked(event["object_id"], ("Email.sent_by_customer",))
        open_orders = []
        if cust:
            heads = [o for o in ctx.linked_many(cust, ("SalesOrder.ordered_by",))
                     if o.startswith("SalesOrder:")]
            rows = ctx.read(heads[:400], ("order_number", "status", "order_date"))
            open_orders = sorted((ap._v(r, "order_date")[:10], ap._v(r, "order_number"), o)
                                 for o, r in rows.items()
                                 if not ap.CLOSED.search(ap._v(r, "status")))[-5:]
        lines, _f = _status_lines(ctx, orders)
        out.update(kind="escalation", recommendation="call_or_reply_today",
                   reasons=lines or ["no order named in the message"],
                   evidence=[{"object_id": o, "order": n, "date": d} for d, n, o in open_orders],
                   summary=f"Escalation from {event.get('sender')}: {subject[:100]}")
        return out
    orders = _current(ctx, orders)
    if orders and ({"status_request", "document_request"} & intents or "?" in own):
        lines, facts = _status_lines(ctx, orders)
        if not lines:
            return {"skip": "named_orders_state_nothing"}
        msg = draft(event.get("sender") or "", subject, lines, facts,
                    in_reply_to=event["object_id"], today=ctx.as_of)
        if msg is None:
            return {"skip": "draft_failed_copy_gate"}
        out.update(kind="answer", summary=f"Answer drafted: {' '.join(lines)[:140]}",
                   draft=msg, links=[{"label": "Open the order", "href": f"/object/{o}"}
                                     for o in orders[:3]])
        return out
    if PRICE_ASK.search(own) and (orders or pos):
        return {"skip": "price_ask_without_price_records"}
    return {"skip": "nothing_to_prepare"}


QUESTION = re.compile(r"[^.?!\n]{8,240}\?")


def h_reply_prep(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """A reply someone is waiting on, in a thread the owner already writes in (or a
    question from someone they write to): what was asked, the records the thread names
    with their current state, and a drafted answer when those records state one. Never
    sent: the owner edits and sends from the card."""
    subject = event.get("subject") or ""
    text = body_text(ctx, event)
    own = text.split("\nFrom:")[0][:4000]
    asked = [q.strip() for q in QUESTION.findall(own)][:3]
    named = ap._records_named(ctx, {"subject": subject, "text": own,
                                    "records": event.get("records") or []})
    pos = [o for o in [*(event.get("records") or ()), *ap.identified(ctx, subject, own)]
           if o.startswith("PurchaseOrder:")]
    records = list(dict.fromkeys([*named, *pos]))[:6]
    orders = _current(ctx, [o for o in records if o.startswith("SalesOrder:")])
    lines, facts = _status_lines(ctx, orders) if orders else ([], {})
    out: dict[str, Any] = {
        "kind": "reply", "asked": asked, "thread": {
            "engaged": bool(event.get("engaged")),
            "correspondent": bool(event.get("correspondent"))},
        "records": [{"object_id": o} for o in records],
        "links": [{"label": "Open the record", "href": f"/object/{o}"} for o in records[:3]],
        "gaps": [] if lines else ["no_record_states_the_answer"]}
    if lines and event.get("channel") == "mail":
        msg = draft(event.get("sender") or "", subject, lines, facts,
                    in_reply_to=event["object_id"], today=ctx.as_of)
        if msg is not None:
            out["draft"] = msg
    elif lines and event.get("channel") == "teams":
        # the same answer, as a chat reply: kept on the item only (``teams_draft``); no
        # card carries it (``autopilot.card_of``), so nothing here is ever posted
        msg = draft("", "", lines, facts, today=ctx.as_of)
        if msg is not None:
            out["teams_draft"] = {"channel": "teams", "conversation": event.get(
                "conversation_id"), "reply_to": event["object_id"], "body": msg["body"],
                "copy_check": msg["copy_check"], "post": False}
    who = event.get("sender") or "someone"
    about = subject[:100] or ("(Teams message)" if event.get("channel") == "teams"
                              else "(no subject)")
    out["summary"] = (f"{who} is waiting on a reply: {about}"
                      + (f" ({len(asked)} question{'s' if len(asked) != 1 else ''})"
                         if asked else ""))
    return out


def brief(items: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """The morning brief: decisions waiting, drafts ready to send, late supplier POs and
    ETA slips, each as counts and the first few titles (oldest first)."""
    def pick(pred: Any) -> list[Mapping[str, Any]]:
        return sorted((i for i in items if pred(i)), key=lambda i: i["created_at"])

    def work(i: Mapping[str, Any]) -> Mapping[str, Any]:
        return (i.get("body") or {}).get("prepared") or {}

    sections = {
        "decisions": pick(lambda i: work(i).get("kind") in ("decision", "escalation") or
                          i["autopilot"] in ("approval_request", "mail_approval_request")),
        "drafts": pick(lambda i: bool(work(i).get("draft"))),
        "orders_to_enter": pick(lambda i: bool(work(i).get("write"))),
        "supplier_followups": pick(lambda i: i["autopilot"] == "po_followup"),
        "eta_slips": pick(lambda i: ((work(i).get("eta") or {}).get("slip_days") or 0) > 0),
    }
    return {"at": ap._iso(now), "counts": {k: len(v) for k, v in sections.items()},
            "top": {k: [{"item_id": i["item_id"], "title": i["title"]} for i in v[:5]]
                    for k, v in sections.items()}}


# ----------------------------------------------------------------------------- invoice send

INVOICE_HEAD = ("invoice_number", "invoice_date", "order_number", "customer_po",
                "customer_number", "total_amount", "currency")
INVOICE_MAILED = "Email.mentions_sales_invoice"
INVOICE_BILLS = "SalesInvoice.billed_to"
PAST_INVOICES = 20


def _outbound(st: Any, mails: Sequence[str], domains: Sequence[str],
              directory: Mapping[str, str], until: str) -> list[tuple[str, str, list[str]]]:
    """(sent at, the internal sender's id, the outside To addresses) of the mails sent
    from inside to outside before ``until``, oldest first."""
    rows = ap._read(st, list(mails)[:60], ("sender", "to_addresses", "sent_at"))
    out = []
    for r in rows.values():
        at = ap._v(r, "sent_at")
        sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
        if not at or at >= until or not ap._internal(sender, domains) or \
                sender not in directory:
            continue
        ext = [a for a in ap._addresses(ap._v(r, "to_addresses"))
               if not ap._internal(a, domains)]
        if ext:
            out.append((at, directory[sender], ext))
    return sorted(out)


def invoice_due_events(st: Any, registry: Mapping[str, Any], as_of: date,
                       domains: Sequence[str], directory: Mapping[str, str]
                       ) -> list[dict[str, Any]]:
    """``record.due`` events: an invoice dated in the last ``window_days`` that no mail
    from inside to the customer has named yet. ``owner_hint``: who mailed this customer's
    latest earlier invoice (and ``customer_to``, to whom): the person the records show
    sending this customer's invoices; none known: the event has no owner. Only what was
    sent before the end of ``as_of`` counts (a replay never reads ahead)."""
    spec = next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == "invoice_send" and a.get("enabled") is not False),
                None)
    if spec is None:
        return []
    window = int((spec.get("params") or {}).get("window_days", 2))
    since = (as_of - timedelta(days=window)).isoformat()
    until = (as_of + timedelta(days=1)).isoformat()
    cutoff = until + "T00:00:00Z"
    from .autopilot_receivables import _due_ids
    ids = _due_ids(st, "SalesInvoice.invoiced", "SalesInvoice", date.fromisoformat(since),
                   as_of)
    rows = ap._read(st, ids, INVOICE_HEAD)
    out = []
    for oid in ids:
        row = rows.get(oid) or {}
        number = ap._v(row, "invoice_number").strip()
        if not number:
            continue
        links = ap._linked(st, oid, (INVOICE_MAILED, INVOICE_BILLS))
        if _outbound(st, [o for t, o in links if t == INVOICE_MAILED], domains, directory,
                     cutoff):
            continue                                   # already sent
        customers = sorted({o for t, o in links if t == INVOICE_BILLS})
        if len(customers) != 1:
            continue
        earlier = [o for t, o in ap._linked(st, customers[0], (INVOICE_BILLS,))
                   if o != oid and o.startswith("SalesInvoice:")]
        dates = ap._read(st, earlier, ("invoice_date",))
        earlier = sorted((d for d in earlier if ap._v(dates.get(d), "invoice_date")
                          and ap._v(dates.get(d), "invoice_date") < cutoff),
                         key=lambda d: ap._v(dates[d], "invoice_date"))[-PAST_INVOICES:]
        mails = [r[3] if r[3].startswith("Email:") else r[4]
                 for r in (st.touching(earlier, (INVOICE_MAILED,)) if earlier else ())]
        sent = _outbound(st, mails[-60:], domains, directory, cutoff)
        owner, to = (sent[-1][1], sent[-1][2][0]) if sent else (None, None)
        out.append({"event_id": ap._digest("record.due", "invoice_send", oid),
                    "event_type": "record.due", "object_id": oid,
                    "object_type": "SalesInvoice", "at": ap._v(row, "invoice_date"),
                    "status": "invoiced", "owner_hint": owner, "customer_to": to,
                    "audience": ap._types(st, [oid]).get(oid, ("", ""))[1],
                    "handles": [c["l1_handle"] for c in row.values()
                                if c.get("l1_handle")][:3],
                    "fields": {"invoice_number": number,
                               "invoice_date": ap._v(row, "invoice_date")[:10]}})
    return out


def h_invoice_send(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """The invoice drafted to the customer contact this customer's invoices went to,
    citing its number, date, order and total. Nothing is sent."""
    inv = event["object_id"]
    head = ctx.read([inv], INVOICE_HEAD).get(inv) or {}
    number = ap._v(head, "invoice_number").strip()
    to = event.get("customer_to")
    if not number:
        return {"skip": "invoice_number_missing"}
    if not to:
        return {"skip": "customer_contact_unknown"}
    ctx.cite(head, "invoice_number", "invoice_date", "total_amount")
    day = ap._v(head, "invoice_date")[:10]
    total = ap._decimal(ap._v(head, "total_amount"))
    currency = ap._v(head, "currency").strip()
    order = ap._v(head, "order_number").strip()
    po = ap._v(head, "customer_po").strip()
    # the total only with its currency (``ontology.measure.money_text``): a bare figure in a
    # customer's email is never written; without one the draft says the total is not stated
    amount = money_text(total, currency) if total is not None else None
    facts = {"invoice": number, "invoice_date": day, "order": order or None,
             "customer_po": po or None, "total": amount}
    ref = f" for your PO {po}" if po else (f" for order {order}" if order else "")
    parts = [f"Please find attached invoice {number}{ref}, dated {say(day, ctx.as_of)}."]
    if amount:
        parts.append(f"The total is {amount}.")
    msg = draft(to, f"Invoice {number}" + (f" - PO {po}" if po else ""), parts, facts,
                today=ctx.as_of)
    if msg is None:
        return {"skip": "draft_failed_copy_gate"}
    return {"summary": f"Invoice {number} dated {day} has not been sent to the customer; "
                       "the email is drafted.",
            "object": {"object_id": inv, "number": number, "date": day, "total": amount},
            "draft": msg, "gaps": [] if amount else ["total not stated"],
            "attach": [{"label": f"Invoice {number}", "object_id": inv}],
            "links": [{"label": "Open the invoice", "href": f"/object/{inv}"}]}


# ----------------------------------------------------------------------------- routine update

ROUTINE_SPREAD_MINUTES = 2      # instances sent within this many minutes of one time of day:
                                # a scheduled send (a rule or flow sends it, no one writes it)


def _routine_cadence(days: Sequence[date], as_of: date, *, min_sends: int,
                     daily_share: float) -> str | None:
    """``daily`` or ``weekly`` when the send days make ``as_of`` a due day, else None."""
    if len(days) < min_sends:
        return None
    if as_of.weekday() < 5 and (as_of - days[-1]).days <= 4:
        span = [days[0] + timedelta(i) for i in range((as_of - days[0]).days)]
        weekdays = [d for d in span if d.weekday() < 5]
        if weekdays and len([d for d in days if d.weekday() < 5]) >= daily_share * len(weekdays):
            return "daily"
    same = [d for d in days if d.weekday() == as_of.weekday() and (as_of - d).days <= 28]
    if len(same) >= min_sends and (as_of - days[-1]).days <= 8:
        return "weekly"
    return None


def routine_due_events(st: Any, queue: Any, registry: Mapping[str, Any], as_of: date,
                       people: Any) -> list[dict[str, Any]]:
    """``record.due`` events: a person's recurring update to colleagues (a subject series
    they started themselves, ``ap.routine_series``) due on ``as_of``: sent on at least
    ``min_sends`` days of the last ``lookback_days``, daily (most weekdays) or weekly (the
    same weekday), written by them (a greeting in most instances) and not a scheduled send
    (instances spread over more than ``ROUTINE_SPREAD_MINUTES`` of the day). To: the
    colleagues on at least half of the instances. Only mails sent before ``as_of`` count (a
    replay never reads ahead). A series with a colleague the people exclusion does not
    clear is left out (fail closed)."""
    spec = next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == "routine_update" and a.get("enabled") is not False),
                None)
    if spec is None or people is None or getattr(people, "unavailable", None):
        return []
    params = spec.get("params") or {}
    look = int(params.get("lookback_days", 28))
    min_sends = int(params.get("min_sends", 3))
    share = float(params.get("daily_share", 0.6))
    start = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
    series: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in queue.routine(ap._iso(start - timedelta(days=look)), ap._iso(start)):
        series[(row["owner"], row["series"])].append(row)
    due = []
    for (owner, key), rows in sorted(series.items()):
        days = sorted({ap._instant(r["at"]).date() for r in rows})
        cadence = _routine_cadence(days, as_of, min_sends=min_sends, daily_share=share)
        if cadence is None:
            continue
        minutes = [ap._instant(r["at"]) for r in rows]
        minutes = [m.hour * 60 + m.minute for m in minutes]
        if max(minutes) - min(minutes) <= ROUTINE_SPREAD_MINUTES:
            continue                                   # a scheduled send
        if sum(r["greeted"] for r in rows) * 2 < len(rows):
            continue                                   # not written by a person
        due.append((owner, key, cadence, rows, days))
    mails = ap._read(st, sorted({r["message"] for *_x, rows, _d in due for r in rows}),
                     ("subject", "to_addresses", "cc_addresses", "sent_at"))
    out = []
    for owner, key, cadence, rows, days in due:
        counts = Counter(d for r in rows for d in json.loads(r["recipients"]))
        named: dict[str, str] = {}
        for r in rows:
            m = mails.get(r["message"]) or {}
            for a in ap._addresses(ap._v(m, "to_addresses")) + ap._addresses(
                    ap._v(m, "cc_addresses")):
                named[ap._digest("party", a.lower())] = a.lower()
        if set(counts) - set(named) or not all(people.identity_clear(named[d])
                                               for d in counts):
            continue                                   # a colleague unknown or not cleared
        to = sorted(named[d] for d, n in counts.items() if n * 2 >= len(rows))
        last = rows[-1]
        subject = ap._v(mails.get(last["message"]), "subject")
        if not to or not subject:
            continue
        out.append({"event_id": ap._digest("record.due", "routine_update", owner, key,
                                           as_of.isoformat()),
                    "event_type": "record.due", "object_id": last["message"],
                    "object_type": "Email", "at": ap._iso(start), "status": cadence,
                    "owner_hint": owner, "series": key, "match_hours": 24,
                    "audience": f"mail:{owner}",
                    "fields": {"cadence": cadence, "sends": len(rows), "days": len(days),
                               "last_sent": last["at"][:10], "last_subject": subject[:200],
                               "recipients": to}})
    return out


def h_routine_update(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Today's instance of a recurring update, drafted to the colleagues it usually goes to
    with the subject of the last one (its dates moved to today), citing that last mail as
    the template. Nothing is sent."""
    f = event.get("fields") or {}
    to = list(f.get("recipients") or ())
    if not to:
        return {"skip": "recipients_unknown"}
    handle = _handle(ctx, event["object_id"])
    if handle:
        ctx.handles.append(handle)
    ctx.objects.add(event["object_id"])
    last_day = str(f.get("last_sent") or "")
    subject = _today_subject(str(f.get("last_subject") or ""), last_day, ctx.as_of)
    facts = {"today": ctx.as_of.isoformat(), "last_sent": last_day,
             "sends": f.get("sends"), "cadence": f.get("cadence")}
    msg = draft(to[0], subject, [f"Here is the update for {say(ctx.as_of.isoformat(), ctx.as_of)}."],
                facts, today=ctx.as_of)
    if msg is None:
        return {"skip": "draft_failed_copy_gate"}
    msg["cc"] = [{"address": a} for a in to[1:]]
    text = body_text(ctx, {"object_id": event["object_id"]}) if handle else ""
    return {"summary": f"Your {f.get('cadence')} update '{subject}' is due today "
                       f"({f.get('sends')} sent on {f.get('days')} days of the last four weeks, "
                       f"the last on {last_day}); the email is drafted to the same colleagues.",
            "object": {"object_id": event["object_id"], "series": event.get("series"),
                       "last_sent": last_day},
            "draft": msg, "template": text[:ap.TEXT_CHARS],
            "gaps": ["the update's content: start from the last one"],
            "links": [{"label": "Open the last one", "href": f"/object/{event['object_id']}"}]}



# ----------------------------------------------------------------------------- R&D status

def rd_due_events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any
                  ) -> list[dict[str, Any]]:
    """``record.due`` events: an R&D or sample project (``operating.rd_projects``: one
    customer or outside party, one kind, one item or conversation) whose latest thread, as
    it stood at the start of ``as_of`` (no later message is read), is in one of the
    autopilot's ``states`` (default: the customer wrote last and asked) and has been quiet
    for ``idle_business_days`` business days or more. One event per project and latest
    message (a project quiet for a week is one piece of work, not one a day); its owner is
    the person of ours who wrote most in the project's threads (``owner_hint``), never a
    role. A thread with a person of ours the exclusion does not clear is never read
    (``rd_projects``: fail closed); the exclusion unavailable: none."""
    from . import proactive_threads as pt
    from . import rd_projects as rd
    spec = next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == "rd_status_update" and a.get("enabled") is not False),
                None)
    if spec is None or people is None or getattr(people, "unavailable", None):
        return []
    params = spec.get("params") or {}
    idle = int(params.get("idle_business_days", rd.IDLE_BUSINESS_DAYS))
    states = set(params.get("states") or ("waiting_on_us",))
    start = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
    pop = rd.population(st, as_of, people)
    counts: Counter = Counter()
    lines = rd.project_lines(st, pop, as_of, people, counts, Counter(), before=start)
    projects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        projects[line["key"]].append(line)
    out = []
    for key, group in sorted(projects.items()):
        group.sort(key=lambda r: (r["last_at"], r["last"]))
        latest = group[-1]
        waited = pt._business_days(latest["last_at"].date(), as_of)
        owners = Counter(r["owner"] for r in group if r.get("owner"))
        owner = max(owners, key=lambda k: (owners[k], k)) if owners else None
        if latest["state"] not in states or waited < idle or not owner or \
                not latest.get("contact"):
            continue
        t = latest["t"]
        out.append({"event_id": ap._digest("record.due", "rd_status_update", key,
                                           t.ident(latest["last"])),
                    "event_type": "record.due", "object_id": latest["last"],
                    "object_type": "Email", "at": ap._iso(start), "status": latest["state"],
                    "owner_hint": owner, "conversation_id": latest["conversation"],
                    "audience": pop["audience"].get(latest["last"]),
                    "records": [latest["customer"]] if latest.get("customer") else [],
                    "fields": {"project": key, "kind": latest["kind"],
                               "state": latest["state"], "days": waited,
                               "last_on": latest["last_at"].date().isoformat(),
                               "first_on": min(r["first_at"] for r in group).date().isoformat(),
                               "threads": len(group), "contact": latest["contact"],
                               "subject": t.subject(latest["last"])[:200]}})
    return out


def h_rd_status_update(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """A status update to the customer of an R&D or sample project that waits on us, as a
    reply in its latest thread, worded from the project's kind, state and dates only (never
    a message's text, a price, a quantity or a product name). Nothing is sent."""
    from . import inbox_triage as it
    from . import rd_projects as rd
    f = event.get("fields") or {}
    to = str(f.get("contact") or "")
    word = rd.KIND_WORD.get(str(f.get("kind") or ""))
    if "@" not in to or word is None:
        return {"skip": "recipients_unknown"}
    handle = _handle(ctx, event["object_id"])
    if handle:
        ctx.handles.append(handle)
    ctx.objects.add(event["object_id"])
    last_on = str(f.get("last_on") or "")
    by = it._next_business_day(ctx.as_of).isoformat()
    facts = {"last_on": last_on, "reply_by": by, "days": f.get("days")}
    msg = draft(to, str(f.get("subject") or f"Your {word}"),
                [f"A quick update on your {word}: we are working on it and will come back to "
                 f"you by {say(by, ctx.as_of)}."], facts,
                in_reply_to=event["object_id"], today=ctx.as_of)
    if msg is None:
        return {"skip": "draft_failed_copy_gate"}
    return {"summary": f"Your customer's {word} has waited on us for {f.get('days')} "
                       f"business day(s) (last message on {last_on}); a status update is "
                       f"drafted as a reply in that thread.",
            "object": {"object_id": event["object_id"], "project": f.get("project"),
                       "kind": f.get("kind"), "state": f.get("state")},
            "draft": msg,
            "gaps": ["what is done and what is next: add it before you send"],
            "links": [{"label": "Open the R&D board", "href": "/rd-projects"},
                      {"label": "Open the last message", "href": f"/object/{event['object_id']}"}]}


_ISO_DAY = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")


def _today_subject(subject: str, last_day: str, today: date) -> str:
    """The last subject with its ISO date (the last send's day) moved to ``today``."""
    return _ISO_DAY.sub(lambda m: today.isoformat() if m.group(0) == last_day else m.group(0),
                        subject)
