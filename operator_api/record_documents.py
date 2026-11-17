"""The documents section of a record's page: the ``business_documents`` lists that name
the record, read as the reader (``plx.aggregate`` mode ``business_documents`` with
``subject``: the aggregate's gate shows a row only when the reader may read every record
it cites and holds its markings; price sheets need ``finance``, else none is read).

Which lists a page has is decided by the record's type (``LISTS``): a supplier's quality
issues; an item's issues, forecasts, forecast accuracy and price sheets; a customer's
forecasts, forecast accuracy, price sheets and the questionnaire answers we gave them.
Each list keeps the aggregate's counts of what it left out (``withheld``) and whether it
is ``complete``; a list the aggregate refused says why, never an empty list in its place.

Rows name other records only as the reader reads them (``cards_words``): a record the
aggregate cited is shown (its gate read it); a record a row only mentions (an issue's item
or lot code) is shown only when the reader's own read of it gives a name. Figures are the
sheet's and the records' exact decimal strings; a percentage is computed in Decimal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

from . import models as m
from .serving import object_url

ITEM_TYPES = ("Product", "FgItem")
# the lists of each record type's page, in page order
LISTS: dict[str, tuple[str, ...]] = {
    "Supplier": ("supplier_issues",),
    "Product": ("supplier_issues", "forecasts", "forecast_accuracy", "price_sheets"),
    "FgItem": ("supplier_issues", "forecasts", "forecast_accuracy", "price_sheets"),
    "Customer": ("forecasts", "forecast_accuracy", "price_sheets", "answer_bank"),
}
TITLES = {"supplier_issues": "Quality issues", "forecasts": "Forecasts they sent",
          "forecast_accuracy": "Forecast against orders", "price_sheets": "Price sheet against invoices",
          "answer_bank": "Questionnaire answers we gave"}
# an issue's kind word (``business_documents.ISSUE_WORDS``) as a reader says it
ISSUE_KIND = {"reject": "Reject", "deviation": "Deviation", "complaint": "Complaint",
              "ncr": "NCR", "nonconform": "Nonconformance", "quarantine": "Quarantine",
              "oos": "Out of spec", "contaminat": "Contamination", "defect": "Defect",
              "issue": "Issue"}
SOURCE_WORDS = {"Email": "Email", "PlannerTask": "Planner task",
                "TeamsChatMessage": "Teams message", "TeamsChannelMessage": "Teams message"}
# a list whose documents the aggregate is still reading (``business_documents.MEMO``):
# the page is not kept with it (``still_reading``), so the next open reads it again
STILL_READING = "Still being read: open the page again in a minute."
FORECAST_MONTHS = 12      # a forecast row's months: the latest twelve
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def applies(object_type: str) -> bool:
    return object_type in LISTS


def _month(period: Any) -> str:
    """'2026-12' as 'Dec 2026'; anything else as given."""
    text = str(period or "")
    try:
        y, mo = text.split("-")
        return f"{_MONTHS[int(mo) - 1]} {int(y)}"
    except (ValueError, IndexError):
        return text


def _dec(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value is not None and str(value).strip() else None
    except InvalidOperation:
        return None


def _qty(value: Any) -> Optional[str]:
    d = _dec(value)
    return None if d is None else format(d.normalize() if d == d.to_integral() else d, "f")


def _percent(ratio: Any, *, signed: bool = False) -> Optional[str]:
    d = _dec(ratio)
    if d is None:
        return None
    text = format((d * 100).quantize(Decimal("0.1")), "f")
    return ("+" + text if signed and d > 0 else text)


class _Names:
    """The records a list names, as the reader reads them (one batched read)."""

    def __init__(self, view: Any, oids: Sequence[str]) -> None:
        from .backend import cards_words
        wanted = [o for o in dict.fromkeys(oids) if isinstance(o, str) and ":" in o]
        try:
            self.words = cards_words(view, wanted) if wanted else {}
        except Exception:  # noqa: BLE001 - no name read: cited ids only, mentioned none
            self.words = {}

    def ref(self, oid: Any, *, cited: bool) -> Optional[m.ObjectRef]:
        """A record the row names: a cited one (the aggregate's gate read it) by its name
        or, unnamed, its id; a merely mentioned one only when the reader's read names it."""
        if not isinstance(oid, str) or ":" not in oid:
            return None
        label = (self.words.get(oid) or ("", None))[0]
        if not label and not cited:
            return None
        kind = oid.split(":", 1)[0]
        return m.ObjectRef(object_id=oid, type=kind, label=label or oid, open_url=object_url(kind, oid))


def _refs(names: _Names, oids: Sequence[Any], cited: set[str], page: str) -> list[m.ObjectRef]:
    """The records a row names, the page's own record left out (the page is it)."""
    out = []
    for oid in dict.fromkeys(o for o in oids if isinstance(o, str)):
        if oid == page:
            continue
        ref = names.ref(oid, cited=oid in cited)
        if ref is not None:
            out.append(ref)
    return out


def _cited(row: Mapping[str, Any]) -> set[str]:
    return {str(c.get("object_id")) for c in row.get("cites") or () if isinstance(c, Mapping)}


def _issue_rows(rows, names, page):
    out = []
    for r in rows:
        cited = _cited(r)
        kind = ISSUE_KIND.get(str(r.get("kind") or "issue"), "Issue")
        records = _refs(names, [*(r.get("suppliers") or ()), *(r.get("items") or ()),
                                *(r.get("lots") or ()), *(r.get("receipts") or ())], cited, page)
        reporter = r.get("reported_by")
        rep = names.ref(reporter, cited=reporter in cited) if reporter and reporter != page \
            else None
        source = names.ref(r.get("record"), cited=True)
        figures = []
        if rep is not None:
            figures.append(m.DocFigure(label="Reported by", value=rep.label))
        out.append(m.DocRow(
            title=str(r.get("title") or kind)[:200],
            dated=r.get("dated"), status=kind, tone="bad" if kind != "Issue" else "warn",
            # the title is the message's own subject or task title: the link says what it is
            source=source.model_copy(update={"label": SOURCE_WORDS.get(str(r.get("source")),
                                                                       "Record")})
            if source else None,
            records=records + ([rep] if rep is not None else []), figures=figures))
    return out


def _forecast_rows(rows, names, page):
    """One row per customer and item: its months, each the newest forecast for it."""
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[(str(r.get("customer")), str(r.get("item")))].append(r)
    out = []
    for (customer, item), lines in grouped.items():
        lines = sorted(lines, key=lambda r: str(r.get("period") or ""))
        newest = max(lines, key=lambda r: (str(r.get("received") or ""), str(r.get("document"))))
        cited = set().union(*(_cited(r) for r in lines))
        units = {r.get("unit") for r in lines}
        figures = [m.DocFigure(label=_month(r.get("period")), value=_qty(r.get("quantity")) or "",
                               unit=r.get("unit") or None)
                   for r in lines[-FORECAST_MONTHS:] if _qty(r.get("quantity")) is not None]
        records = _refs(names, [customer, item], cited, page)
        title = next((x.label for x in records if x.type in ITEM_TYPES), None) \
            or next((x.label for x in records), "Forecast")
        first, last = _month(lines[0].get("period")), _month(lines[-1].get("period"))
        span = first if first == last else f"{first} to {last}"
        out.append(m.DocRow(
            title=title, dated=newest.get("received"),
            detail=f"{len(lines)} month{'s' if len(lines) != 1 else ''}, {span}"
            + ("" if len(lines) <= FORECAST_MONTHS else f"; the latest {FORECAST_MONTHS} below")
            + ("; unit not stated on the sheet" if units == {None} else ""),
            status="Unit stated" if None not in units else "Unit not stated",
            source=names.ref(newest.get("document"), cited=True),
            records=records, figures=figures))
    out.sort(key=lambda r: (r.dated or "", r.title), reverse=True)
    return out


def _accuracy_rows(rows, names, page):
    out = []
    for r in rows:
        cited = _cited(r)
        months = [x for x in r.get("months") or () if isinstance(x, Mapping)]
        unit = r.get("order_unit")
        wape, bias = _percent(r.get("wape")), _percent(r.get("bias"), signed=True)
        figures = []
        if wape is not None:
            figures.append(m.DocFigure(label="Error (weighted)", value=wape, unit="%"))
        if bias is not None:
            figures.append(m.DocFigure(label="Bias", value=bias, unit="%"))
        figures += [m.DocFigure(label="Forecast", value=_qty(r.get("forecast_total")) or "0",
                                unit=unit),
                    m.DocFigure(label="Ordered", value=_qty(r.get("actual_total")) or "0",
                                unit=unit)]
        b = _dec(r.get("bias"))
        status = ("No orders in the months" if b is None else "Over-forecast" if b > 0
                  else "Under-forecast" if b < 0 else "On forecast")
        records = _refs(names, [r.get("customer"), r.get("item")], cited, page)
        title = next((x.label for x in records if x.type in ITEM_TYPES), None) \
            or next((x.label for x in records), "Forecast")
        last = months[-1] if months else {}
        out.append(m.DocRow(
            title=title,
            detail=f"{len(months)} ended month{'s' if len(months) != 1 else ''} scored"
            + (f", {_month(months[0].get('period'))} to {_month(last.get('period'))}"
               if len(months) > 1 else "")
            + ("" if r.get("unit_basis") == "stated" else "; the sheet states no unit"),
            dated=None, status=status,
            tone=None if b is None else "warn" if abs(b) >= Decimal("0.2") else "good",
            source=names.ref(last.get("document"), cited=True) if last else None,
            records=records, figures=figures))
    return out


def _price_rows(rows, names, page):
    out = []
    for r in rows:
        cited = _cited(r)
        cur = r.get("currency")
        off = int(r.get("mismatched_lines") or 0)
        n = int(r.get("invoice_lines") or 0)
        figures = [m.DocFigure(label="Sheet price", value=str(r.get("sheet_price")), unit=cur),
                   m.DocFigure(label="Furthest billed" if off else "Billed",
                               value=str(r.get("worst_invoiced_price")), unit=cur),
                   m.DocFigure(label="Invoice lines off", value=f"{off} of {n}")]
        records = _refs(names, [r.get("customer"), r.get("item")], cited, page)
        title = next((x.label for x in records if x.type in ITEM_TYPES), None) \
            or next((x.label for x in records), "Price sheet")
        day = r.get("worst_invoice_day")
        notes = [f"furthest line billed {day}" if off and day else "",
                 "" if r.get("currency_basis") == "stated" else
                 "the sheet states no currency, so it is read in the invoices' one currency"]
        detail = "; ".join(n for n in notes if n)
        out.append(m.DocRow(
            title=title, dated=r.get("sheet_day"),
            detail=detail[:1].upper() + detail[1:] if detail else None,
            status="Billed off the sheet" if r.get("status") == "mismatch" else "Billed as the sheet",
            tone="bad" if r.get("status") == "mismatch" else "good",
            source=names.ref(r.get("document"), cited=True), records=records, figures=figures))
    return out


def _answer_rows(rows, names, page):
    out = []
    for r in rows:
        cited = _cited(r)
        specific = r.get("scope") == "customer_specific"
        out.append(m.DocRow(
            title=str(r.get("question") or "")[:300], detail=str(r.get("answer") or "")[:500],
            dated=r.get("answered"),
            status="For this customer only" if specific else "Any customer",
            source=names.ref(r.get("document"), cited=True),
            records=_refs(names, [r.get("party_id")], cited, page)))
    return out


SHAPE: dict[str, Callable[..., list[m.DocRow]]] = {
    "supplier_issues": _issue_rows, "forecasts": _forecast_rows,
    "forecast_accuracy": _accuracy_rows, "price_sheets": _price_rows,
    "answer_bank": _answer_rows}


def _names_in(view_name: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    keys = {"supplier_issues": ("record", "suppliers", "items", "lots", "receipts",
                                "reported_by"),
            "forecasts": ("customer", "item", "document"),
            "forecast_accuracy": ("customer", "item"),
            "price_sheets": ("customer", "item", "document"),
            "answer_bank": ("party_id", "document")}[view_name]
    out: list[str] = []
    for r in rows:
        for k in keys:
            v = r.get(k)
            out += [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str)]
        if view_name == "forecast_accuracy":
            out += [str(x.get("document")) for x in r.get("months") or ()
                    if isinstance(x, Mapping) and x.get("document")]
    return out


def doc_list(view: Any, view_name: str, oid: Optional[str], today: Optional[date], *,
             question: Optional[str] = None) -> m.DocList:
    """One list for one record (``oid``; None: the whole list, as the Templates page reads
    the answer bank), read as this reader."""
    out = m.DocList(view=view_name, title=TITLES[view_name])
    args: dict[str, Any] = {"mode": "business_documents", "view": view_name}
    if oid:
        args["subject"] = oid
    if question:
        args["question"] = question[:700]
    if today is not None:
        args["as_of"] = today.isoformat()
    got = view._call("plx.aggregate", args)
    if not isinstance(got, Mapping):
        out.error, out.complete = "The list could not be read.", False
        return out
    if got.get("error"):
        out.error, out.complete = "The list could not be read.", False
        return out
    if (got.get("counts") or {}).get("still_reading"):
        # its documents are being read for the whole company now, kept for the next page
        out.error, out.complete = STILL_READING, False
        return out
    withheld = {k: int(v) for k, v in (got.get("withheld_counts") or {}).items()
                if isinstance(v, int)}
    if got.get("withheld") == "people_exclusion_unavailable":
        out.error, out.complete = ("Not read: the people exclusion list could not be "
                                   "loaded, so no message is read until it is."), False
        return out
    if view_name == "price_sheets" and withheld.get("withheld_finance") and not got.get("rows"):
        out.needs = "finance"
        return out
    rows = [r for r in got.get("rows") or () if isinstance(r, Mapping)]
    names = _Names(view, _names_in(view_name, rows))
    out.rows = SHAPE[view_name](rows, names, oid or "")
    out.withheld = sum(withheld.values())
    out.complete = bool(got.get("complete", True))
    return out


def answer_bank(view: Any, question: Optional[str], today: Optional[date]) -> m.DocList:
    """The Templates page's answer bank: the answers we gave that any customer may get
    (an answer specific to one customer is only on that customer's page, counted here),
    ranked against ``question`` when one is given."""
    with view._request():
        out = doc_list(view, "answer_bank", None, today, question=question)
    out.title = "Answers any customer may get"
    return out


def still_reading(documents: Optional[m.RecordDocuments]) -> bool:
    """A list of the section is still being read: the section must not be kept."""
    return documents is not None and any(d.error == STILL_READING for d in documents.lists)


def page_documents(view: Any, oid: str, object_type: str,
                   today: Optional[date] = None) -> Optional[m.RecordDocuments]:
    """The record's lists (``LISTS``) in page order; None for a type with none."""
    names = LISTS.get(object_type)
    if not names:
        return None
    return m.RecordDocuments(lists=[doc_list(view, n, oid, today) for n in names])
