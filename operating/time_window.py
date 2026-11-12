"""The time window a question names: "in the last 90 days", "this quarter", "last month",
"since March 3", "year to date", read into exact business days.

One grammar for every windowed answer (``operating.fast_answer``'s receipt counts and
invoiced totals): a window is ``Window(start, end, said)``, both days inclusive in the
business day (``BUSINESS_TZ``), never past today; ``said`` is the window as the answer
states it, with its exact days ("from Aug 5 through Nov 3", "this quarter (Oct 1 through
Nov 3)"), so a reader always sees which days were counted.

* ``last|past N days|weeks|months``: rolling, the N days (7N for weeks, the N months to
  the same day) ending with today, today counted (``last 30 days`` on 2026-11-03 is the 30
  days 2026-10-05 to 2026-11-03; ``last 3 months`` is 2026-08-04 to 2026-11-03), BIZ-009.
* ``this week|month|quarter|year`` (and ``month|quarter|year to date``, ``mtd``, ``qtd``,
  ``ytd``): the calendar period's first day to today; a week starts on Monday.
* ``last|previous week|month|quarter|year``: the whole calendar period before this one;
  ``past week|month|year`` or ``the last week|month|year``: rolling, as ``last 1 month``.
* ``today``, ``yesterday``; ``since <date>`` (ISO, ``March 3``, ``Mar 3 2026``,
  ``3/3/2026`` month first) to today; ``in <month> [<year>]`` that month (a month not yet
  reached this year is last year's), through today when it is this month.

Fail closed: a frame this grammar does not read (two frames, ``between``, ``before``,
a date that does not exist or a calendar cannot hold, such as year 0 or a month past
9999, a future window) is None, never a guess (BIZ-008).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from typing import Optional

BUSINESS_TZ = "America/New_York"
MAX_DAYS = 3660          # a window longer than about ten years is not read
_MONTHS = {m: i for i, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), 1) for m in names}
_MONTH = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_UNIT = r"(days?|weeks?|wks?|months?|mos?)"
_PERIOD = r"(week|month|quarter|qtr|year|yr)"

_PATTERNS = (
    ("rolling", re.compile(rf"\b(?:in the |over the |during the |for the |within the )?"
                           rf"(?:last|past|previous|prior|trailing)\s+(\d{{1,4}})\s+{_UNIT}\b")),
    ("to_date", re.compile(r"\b(month|quarter|year)[- ]to[- ]date\b|\b(mtd|qtd|ytd)\b")),
    ("this", re.compile(rf"\b(?:this|current|the current)\s+{_PERIOD}\b(?:\s+so far)?")),
    ("previous", re.compile(rf"\b(?:last|previous|prior)\s+{_PERIOD}\b")),
    ("past", re.compile(rf"\b(?:(?:the\s+)?past|the\s+last)\s+{_PERIOD}\b")),
    ("day", re.compile(r"\b(today|yesterday)\b")),
    ("since", re.compile(rf"\bsince\s+(?:(\d{{4}})[- ](\d{{2}})[- ](\d{{2}})|({_MONTH})\s+(\d{{1,2}})"
                         rf"(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?|(\d{{1,2}})[/ ](\d{{1,2}})[/ ](\d{{4}})"
                         rf"|({_MONTH})(?:\s+(\d{{4}}))?(?!\s*\d))")),
    ("in_month", re.compile(rf"\b(?:in|during|for)\s+({_MONTH})(?:\s+(\d{{4}}))?(?!\s*\d)")),
)
# Words of a frame this grammar does not read: their question is not windowed here.
_UNREAD = re.compile(r"\b(?:between|before|after|until|till|ago|next|upcoming|coming|"
                     r"tomorrow|from \d|to \d|through|thru)\b")


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    said: str          # the window in words, with its exact days
    span: tuple[int, int]   # where the question wrote it (in the folded text)

    def holds(self, day: date) -> bool:
        return self.start <= day <= self.end


_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
                    r"(Z|[+-]\d{2}(?::?\d{2})?)?)?")
_ZERO_OFFSET = re.compile(r"Z|[+-]00(?::?00)?")


def business_day(value: object) -> Optional[date]:
    """The business day (``BUSINESS_TZ``) a stored date or timestamp names (C-09), the
    one rule every day-of helper reads a value by:

    * a date (``2026-03-01``), or a timestamp at exactly midnight with no zone or a zero
      offset (a day a source wrote as a timestamp: a Prisma date picker, Graph's due
      dates, BC's dates): the written day, never the day before in Eastern time;
    * a timestamp with a zone: the Eastern day of that instant (``02:00Z`` is the evening
      before);
    * a timestamp without a zone: a UTC instant (Prisma writes UTC zoneless; the
      projection declares each source's zone, so a store value is zoned once migrated),
      its Eastern day;
    * a ``datetime``: its Eastern day when aware, its own date when naive; a ``date``:
      itself. Anything else whose first ten characters are an ISO date keeps that date
      (the old helpers' reading); otherwise None."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(_business_zone()).date()
    if isinstance(value, date):
        return value
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    match = _STAMP.fullmatch(text)
    if match is None:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    try:
        written = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
    if match[4] is None:
        return written
    zone = match[8]
    midnight = (match[4], match[5], match[6] or "00") == ("00", "00", "00") and \
        not (match[7] or "").strip("0")
    if midnight and (zone is None or _ZERO_OFFSET.fullmatch(zone)):
        return written
    stamp = text.replace(" ", "T", 1)
    if zone is None:
        stamp += "+00:00"
    elif zone == "Z":
        stamp = stamp[:-1] + "+00:00"
    elif len(zone) == 3:
        stamp += ":00"
    elif ":" not in zone:
        stamp = stamp[:-2] + ":" + stamp[-2:]
    if match[7] is not None and len(match[7]) > 6:
        stamp = stamp.replace("." + match[7], "." + match[7][:6], 1)
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment.astimezone(_business_zone()).date()


def _business_zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(BUSINESS_TZ)


def business_today(now: Optional[datetime] = None) -> date:
    from zoneinfo import ZoneInfo
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(BUSINESS_TZ)).date()


def _months_back(day: date, months: int) -> date:
    month = day.month - 1 - months
    year, month = day.year + month // 12, month % 12 + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def _period_start(today: date, period: str) -> date:
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    if period == "quarter":
        return today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
    return today.replace(month=1, day=1)


def _norm_period(word: str) -> str:
    return {"qtr": "quarter", "yr": "year", "wk": "week", "wks": "week"}.get(word, word)


def _day(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# A whole calendar day a text writes with its year: ISO ``2026-11-10``, ``November 10,
# 2026`` / ``Nov 10th 2026``, ``10 November 2026``, ``11/10/2026`` (month first, as
# ``since`` reads it). A day without its year, or one that does not exist, is none.
_WRITTEN_DAY = re.compile(
    rf"(?<![\w-])(?:(\d{{4}})-(\d{{2}})-(\d{{2}})(?![\w-])"
    rf"|({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\w)"
    rf"|(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH}),?\s+(\d{{4}})(?!\w)"
    rf"|(\d{{1,2}})/(\d{{1,2}})/(\d{{4}})(?![\w/]))")


def written_days(text: str) -> frozenset[date]:
    """The calendar days ``text`` writes in full, its year included (``_WRITTEN_DAY``)."""
    out: set[date] = set()
    if not isinstance(text, str):
        return frozenset()
    for m in _WRITTEN_DAY.finditer(text.casefold()):
        if m[1]:
            day = _day(int(m[1]), int(m[2]), int(m[3]))
        elif m[4]:
            day = _day(int(m[6]), _MONTHS[m[4].rstrip(".")], int(m[5]))
        elif m[8]:
            day = _day(int(m[9]), _MONTHS[m[8].rstrip(".")], int(m[7]))
        else:
            day = _day(int(m[12]), int(m[10]), int(m[11]))
        if day is not None:
            out.add(day)
    return frozenset(out)


def reference_days(question: str, now: Optional[datetime] = None) -> list[str]:
    """The days a ``days`` value claim may count from without citing them (ISO, sorted):
    each day the question writes in full (``written_days``: "days remaining as of
    November 10, 2026") and today in the business day."""
    days = written_days(question) | {business_today(now)}
    return sorted(day.isoformat() for day in days)


def _day_said(day: date, today: date) -> str:
    """A day as the answer writes it: "Aug 5" (its year too when it is not this year's).
    The window is the question's reading, not a record's value: it is written as month and
    day, which the answer gate reads as no figure of a record (a year-dated day it reads as
    one, and that answer is then the agent's)."""
    said = f"{day.strftime('%b')} {day.day}"
    return said if day.year == today.year else f"{said}, {day.year}"


def _range(start: date, end: date, today: date) -> str:
    return f"{_day_said(start, today)} through {_day_said(end, today)}"


def window(folded: str, now: Optional[datetime] = None) -> Optional[Window]:
    """The one window ``folded`` (a folded question: lower case) names, or None: a frame
    whose days a calendar cannot hold (year 0, a month past 9999) is None (BIZ-008)."""
    try:
        return _window(folded, now)
    except (ValueError, OverflowError):
        return None


def _window(folded: str, now: Optional[datetime] = None) -> Optional[Window]:
    if not isinstance(folded, str) or _UNREAD.search(folded):
        return None
    found = [(kind, m) for kind, pattern in _PATTERNS for m in pattern.finditer(folded)]
    # a frame matched by two patterns at once ("in the last 3 months" is rolling, never
    # "last month") counts as the first, longest one
    found.sort(key=lambda km: (km[1].start(), -(km[1].end() - km[1].start())))
    kept: list = []
    for kind, m in found:
        if kept and m.start() < kept[-1][1].end():
            continue
        kept.append((kind, m))
    if len(kept) != 1:
        return None
    kind, m = kept[0]
    today = business_today(now)
    said_as = m.group(0).strip()
    if kind == "rolling":
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            return None
        if unit.startswith("d"):
            start = today - timedelta(days=n - 1)
        elif unit.startswith("w"):
            start = today - timedelta(days=7 * n - 1)
        else:
            start = _months_back(today, n) + timedelta(days=1)
        end = today
        said_as = ""
    elif kind == "to_date":
        period = m.group(1) or {"mtd": "month", "qtd": "quarter", "ytd": "year"}[m.group(2)]
        start, end = _period_start(today, period), today
        said_as = f"{period} to date"
    elif kind == "this":
        period = _norm_period(m.group(1))
        start, end = _period_start(today, period), today
        said_as = f"this {period}"
    elif kind == "previous":
        period = _norm_period(m.group(1))
        end = _period_start(today, period) - timedelta(days=1)
        start = _period_start(end, period)
        said_as = f"last {period}"
    elif kind == "past":
        period = _norm_period(m.group(1))
        start = {"week": today - timedelta(days=6),
                 "month": _months_back(today, 1) + timedelta(days=1),
                 "quarter": _months_back(today, 3) + timedelta(days=1),
                 "year": _months_back(today, 12) + timedelta(days=1)}[period]
        end = today
        said_as = f"in the past {period}"
    elif kind == "day":
        start = end = today - timedelta(days=1 if m.group(1) == "yesterday" else 0)
        said_as = m.group(1)
    elif kind == "since":
        g = m.groups()
        if g[0]:
            start = _day(int(g[0]), int(g[1]), int(g[2]))
        elif g[3]:
            month = _MONTHS[g[3].rstrip(".")]
            year = int(g[5]) if g[5] else today.year
            start = _day(year, month, int(g[4]))
            if start and not g[5] and start > today:
                start = _day(year - 1, month, int(g[4]))
        elif g[6]:
            start = _day(int(g[8]), int(g[6]), int(g[7]))
        else:
            month = _MONTHS[g[9].rstrip(".")]
            year = int(g[10]) if g[10] else today.year
            if not g[10] and month > today.month:
                year -= 1
            start = _day(year, month, 1)
        if start is None:
            return None
        end = today
        said_as = "since"
    else:   # in_month
        month = _MONTHS[m.group(1).rstrip(".")]
        year = int(m.group(2)) if m.group(2) else today.year
        if not m.group(2) and month > today.month:
            year -= 1
        start = _day(year, month, 1)
        if start is None:
            return None
        end = min(_months_back(start, -1) - timedelta(days=1), today)
        said_as = f"in {start.strftime('%B')}"
    if start > end or end > today or (end - start).days > MAX_DAYS:
        return None
    if said_as == "since":
        said = f"since {_day_said(start, today)} (through {_day_said(end, today)})"
    elif not said_as:
        said = f"from {_range(start, end, today)}"
    elif start == end:
        said = f"{said_as} ({_day_said(start, today)})"
    else:
        said = f"{said_as} ({_range(start, end, today)})"
    return Window(start, end, said, (m.start(), m.end()))


__all__ = ["BUSINESS_TZ", "Window", "business_today", "window"]
