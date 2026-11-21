"""When a commitment is due, in the person's words (``due_of``), read for ``memory``."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

_EASTERN = None


def _eastern(moment: datetime) -> datetime:
    global _EASTERN
    if _EASTERN is None:
        from zoneinfo import ZoneInfo
        _EASTERN = ZoneInfo("America/Toronto")
    return moment.astimezone(_EASTERN)


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|" \
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DUE_PATTERNS = (
    ("iso", re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")),
    ("month_day", re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)),
    ("day_month", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b",
                             re.I)),
    ("numeric", re.compile(r"(?<![\w/.-])(\d{1,2})/(\d{1,2})(?![\w/.-])(?!\s+of\b)")),
    ("relative", re.compile(
        r"\b(today|tonight|this (?:morning|afternoon|evening)|end of (?:the )?day|eod|"
        r"tomorrow|end of (?:the |this )?week|eow|end of (?:the |this )?month|next week|"
        r"in (\d{1,2}|a|one|two|three) (days?|weeks?))\b", re.I)),
    ("weekday", re.compile(r"\b(next\s+)?(" + "|".join(_WEEKDAYS) + r"|tues|thurs|fri)\b", re.I)),
)
_SMALL = {"a": 1, "one": 1, "two": 2, "three": 3}


def due_of(text: Any, said_at: Any) -> Optional[str]:
    """The day (``YYYY-MM-DD``, US Eastern) a commitment says it is due, read against when
    it was said: an explicit date ("Nov 20", "20 November", "11/20", "2026-11-20"), "today",
    "tomorrow", a weekday ("Friday": the next one after the day said; "next Friday": the
    one in the week after), "end of the week" (that Friday), "next week" (its Monday),
    "end of the month", "in 3 days". None when it says no day (never guessed)."""
    if not isinstance(text, str) or not text:
        return None
    if isinstance(said_at, str):
        try:
            said_at = datetime.fromisoformat(said_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(said_at, datetime):
        return None
    if said_at.tzinfo is None:
        said_at = said_at.replace(tzinfo=timezone.utc)
    day = _eastern(said_at).date()
    found: list[tuple[int, date]] = []
    for name, pattern in _DUE_PATTERNS:
        for hit in pattern.finditer(text):
            got = _due_hit(name, hit, day)
            if got is not None:
                found.append((hit.start(), got))
        if found and name != "relative":
            break   # an explicit date wins over a weekday said beside it
    if not found:
        return None
    return min(found)[1].isoformat()


def _year_for(month: int, dom: int, day: date) -> Optional[date]:
    for year in (day.year, day.year + 1):
        try:
            got = date(year, month, dom)
        except ValueError:
            continue   # "Feb 29" in a year without one is next leap year's, or none
        if got >= day - timedelta(days=7):   # "Nov 10" said on Nov 12 is last week's
            return got
    return None


def _due_hit(name: str, hit: "re.Match[str]", day: date) -> Optional[date]:
    try:
        if name == "iso":
            return date(int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
        if name in ("month_day", "day_month"):
            month_word, dom = (hit.group(1), hit.group(2)) if name == "month_day" else \
                (hit.group(2), hit.group(1))
            month = next(i for i, m in enumerate(_MONTHS, 1)
                         if m.startswith(month_word.lower().rstrip(".")[:3]))
            if month_word.lower() == "may" and month_word != "May":
                return None   # "we may 4 ..." is a verb; the month is written "May"
            return _year_for(month, int(dom), day)
        if name == "numeric":   # month/day, as written in North America
            return _year_for(int(hit.group(1)), int(hit.group(2)), day)
    except (ValueError, StopIteration):
        return None
    word = hit.group(0).lower()
    if name == "relative":
        if word in ("today", "tonight", "eod") or word.startswith(("this ", "end of the day",
                                                                   "end of day")):
            return day
        if word == "tomorrow":
            return day + timedelta(days=1)
        if "week" in word and word.startswith(("end", "eow")):
            return day + timedelta(days=(4 - day.weekday()) % 7)
        if word.startswith("end of") and "month" in word:
            nxt = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
            return nxt - timedelta(days=1)
        if word == "next week":
            return day + timedelta(days=7 - day.weekday())
        if word.startswith("in "):
            n = hit.group(2)
            n = int(n) if n.isdigit() else _SMALL[n.lower()]
            return day + timedelta(days=n * (7 if hit.group(3).lower().startswith("week") else 1))
        return None
    # a weekday (full names, "tues", "thurs", "fri": "mon", "wed", "sat", "sun" are words)
    target = next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(hit.group(2).lower()[:3]))
    if hit.group(1):   # "next Friday": the one in the week after this one
        return day + timedelta(days=7 - day.weekday() + target)
    return day + timedelta(days=(target - day.weekday()) % 7 or 7)
