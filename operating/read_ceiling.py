"""A read ceiling for replaying a past moment: with ``PLX_READ_CEILING`` set (an ISO time
with its zone), the model tools read as if it were that moment, so a replay of what the
autopilot would have done then sees nothing that happened after it (the person's own
reply, a later message, an order keyed later). Only the replay harness sets it, in the
session it starts; a live service never does, and if one did it would only see less.

What is bounded, by the time each thing happened (never by when it was captured: a
backfilled mail of July was captured in October and is still July's):

* mail and Teams messages, meetings and transcripts: their own time, strictly before the
  ceiling (a meeting also once its invitation, same subject, reached that mailbox before
  it; ``plx.search``'s every leg gets ``until`` no later than it; ``plx.open`` of a later
  message answers not found);
* the business tables with a document date (orders, purchase orders, invoices, their
  lines) and the generic ``records``: a row dated on or after the ceiling's business day
  (America/New_York) is not there, a same-day row included (the day is all a document
  date says, so the whole day is left out);
* ``plx.sql``'s describe shows no sample rows.

Not bounded (said, not hidden): the current values of a record that existed before the
moment (a status or a date changed since reads as it is now) and the tables without a
date (customers, products, stock, balances, staff). A replay grades those reads apart.

A ceiling that is set but unreadable refuses every read (fail closed)."""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

CEILING_ENV = "PLX_READ_CEILING"
BUSINESS_TZ = ZoneInfo("America/New_York")

# view -> the predicate keeping a row known before the ceiling: %(at)s the ceiling as a
# timestamptz literal, %(day)s its business day as a date literal, %(g)s the views' schema
_MESSAGES = {
    "emails": "coalesce(received_at, sent_at) < {at}",
    "teams_messages": "created_at < {at}",
    # a meeting is known once it started, or once its invitation reached that calendar's
    # mailbox in the 180 days before (an upcoming meeting is known from its invitation:
    # answering it, a day's start reading today's meetings)
    "meetings": ("(start_at < {at} OR (mailbox, lower(subject)) IN (SELECT e.mailbox, "
                 "lower(regexp_replace(coalesce(e.subject, ''), '^[[:space:]]*(((re|fw|fwd|"
                 "invitation|updated invitation)[[:space:]]*:[[:space:]]*)+)', '', 'i')) "
                 "FROM {g}.emails e WHERE e.received_at < {at} AND e.received_at > {at} - "
                 "interval '180 days' AND e.mailbox IN (SELECT DISTINCT m.mailbox FROM "
                 "{g}.meetings m)))"),
    "meeting_transcripts": "coalesce(created_at, meeting_start) < {at}",
}
_DATED = {
    "sales_orders": "order_date < {day}",
    "sales_order_lines": "order_date < {day}",
    "purchase_orders": "order_date < {day}",
    "purchase_order_lines": ("NOT EXISTS (SELECT 1 FROM {g}.purchase_orders p WHERE "
                             "p.po_number = purchase_order_lines.po_number AND "
                             "(p.order_date IS NULL OR p.order_date >= {day}))"),
    "sales_invoices": "invoice_date < {day}",
    "sales_invoice_lines": "invoice_date < {day}",
    "supplier_invoices": "document_date < {day}",
    "records": "left(coalesce(record_date, first_seen), 10) < {day_text}",
}
BOUNDED = {**_MESSAGES, **_DATED}


class CeilingInvalid(ValueError):
    """The ceiling is set but is no ISO time with a zone: every read is refused."""


def ceiling(environ: Optional[Mapping[str, str]] = None) -> Optional[dt.datetime]:
    """The ceiling (UTC) or None when none is set; ``CeilingInvalid`` when it cannot be
    read."""
    raw = str((os.environ if environ is None else environ).get(CEILING_ENV) or "").strip()
    if not raw:
        return None
    try:
        moment = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise CeilingInvalid(f"{CEILING_ENV} is not an ISO time") from None
    if moment.tzinfo is None:
        raise CeilingInvalid(f"{CEILING_ENV} needs a time zone")
    return moment.astimezone(dt.timezone.utc)


def day_of(moment: dt.datetime) -> dt.date:
    """The business day the ceiling falls on."""
    return moment.astimezone(BUSINESS_TZ).date()


def _instant(value: Any, *, end: bool) -> Optional[dt.datetime]:
    """A tool's since/until (a date or an ISO time) as an instant; a bare date at its end
    when ``end`` (``until`` covers its whole day, as the tools read it)."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = dt.date.fromisoformat(text)
        if end:
            day += dt.timedelta(days=1)
        return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def clamp_until(until: Any, moment: dt.datetime) -> str:
    """A search's ``until`` no later than the ceiling: the caller's when it is earlier,
    else the ceiling (an ISO time). An unreadable ``until`` is replaced by the ceiling."""
    try:
        asked = _instant(until, end=True)
    except ValueError:
        asked = None
    if asked is not None and asked <= moment:
        return str(until)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def after(at: Any, moment: dt.datetime) -> bool:
    """Whether a message time ``at`` (a datetime or ISO text) is at or after the ceiling;
    an unreadable time counts as after (fail closed)."""
    if isinstance(at, dt.datetime):
        when = at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)
    else:
        try:
            when = _instant(at, end=False)
        except ValueError:
            return True
        if when is None:
            return True
    return when >= moment


def bounded_sql(sql: str, moment: dt.datetime, schema: str) -> str:
    """``sql`` with every bounded view it names read through a same-named common table
    of the rows known before ``moment`` (a common table shadows a bare view name in the
    whole statement, its subqueries included). The caller has refused schema-qualified
    names (they would read past the ceiling)."""
    at = "'" + moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00'::timestamptz"
    day_iso = day_of(moment).isoformat()
    day = f"'{day_iso}'::date"
    named = [v for v in BOUNDED if re.search(rf"\b{v}\b", sql, re.I)]
    if not named:
        return sql
    tables = ", ".join(
        f"{v} AS (SELECT * FROM {schema}.{v} WHERE "
        + BOUNDED[v].format(at=at, day=day, day_text=f"'{day_iso}'", g=schema) + ")"
        for v in named)
    return f"WITH {tables}\nSELECT * FROM (\n{sql}\n) AS plx_ceiling"


__all__ = ["BOUNDED", "CEILING_ENV", "CeilingInvalid", "after", "bounded_sql", "ceiling",
           "clamp_until", "day_of"]
