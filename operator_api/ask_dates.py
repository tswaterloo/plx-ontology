"""One date style in what the Ask shows: the app's own ("21 Nov 2026", "21 Nov" when the
reply gives no year, "Nov 2026" for a month). A reply's ISO dates ("2026-11-21"), ISO
months ("2026-11") and US-ordered dates ("Nov 21, 2026", "Mon Nov 9", "Nov 16-20") are
rewritten in place; nothing else in the text changes, and a code, path or URL holding
digits is left as it is (``dated``)."""

from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo

ZONE = "America/Toronto"

_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FULL = ("January February March April May June July August September October November "
         "December").split()
# an ISO date (a timestamp's day; its time kept when it is not midnight), never part of a
# longer code ("INV-2026-11-21-3"), a path or a URL
_ISO = re.compile(r"(?<![\w/.:-])(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
                  r"(?:[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
                  r"(?![\w/-]|\.\d)")
_ISO_MONTH = re.compile(r"(?<![\w/.:-])(\d{4})-(0[1-9]|1[0-2])(?![\w/:-]|\.\d)")
_MONTH = (r"(January|February|March|April|May|June|July|August|September|October|"
          r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)")
_DAY = r"(3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?"
# words that may follow a date; any other plural word after "May 5" makes it a count
# ("May 5 orders arrived", "Jan 12 % of sales" stay as written)
_GLUE = r"(?:as|is|its|has|was|this|thus|ends|starts|runs)"
# "Nov 21", "Nov 21, 2026", "Nov 16-20", "Mon Nov 9" (the weekday kept before the day)
_US = re.compile(
    rf"(?<![\w-])(?:(Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)\.?,?\s+)?{_MONTH}\.?\s+"
    rf"{_DAY}(?:\s?([-–])\s?{_DAY})?(?:,?\s+((?:19|20)\d{{2}}))?"
    r"(?![\d:%]|[.,]\d|\s?(?:am|pm|AM|PM)\b|\s?%|\s+(?!" + _GLUE + r"\b)[a-z]+s\b)")


def _iso(found: re.Match) -> str:
    year, month, day = int(found.group(1)), int(found.group(2)), int(found.group(3))
    hour, minute = found.group(4), found.group(5)
    try:
        datetime(year, month, day)
    except ValueError:
        return found.group(0)   # no such day: not a date
    zoned = re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", found.group(0))
    # a midnight written as a timestamp is its own day (a period's start)
    if zoned and hour is not None and (hour, minute) != ("00", "00"):
        # a zoned timestamp is said in the business time zone (02:00Z on 1 Nov is 31 Oct)
        try:
            stamp = datetime.fromisoformat(found.group(0).replace("Z", "+00:00").replace(" ", "T"))
            local = stamp.astimezone(ZoneInfo(ZONE))
            year, month, day = local.year, local.month, local.day
            hour, minute = f"{local.hour:02d}", f"{local.minute:02d}"
        except ValueError:
            return found.group(0)
    out = f"{day} {_SHORT[month - 1]} {year}"
    if hour is not None and (hour, minute) != ("00", "00"):
        out += f" {hour}:{minute}"
    return out


def _us(found: re.Match) -> str:
    weekday, month, day, dash, last, year = found.groups()
    word = month if month in _FULL else _SHORT[[m[:3] for m in _SHORT].index(month[:3])]
    days = f"{int(day)}{dash}{int(last)}" if dash else f"{int(day)}"
    return " ".join(p for p in (weekday, days, word, year) if p)


def dated(text: str) -> str:
    """``text`` with every date in the app's style (module doc)."""
    if not text or not re.search(r"\d", text):
        return text
    out = _ISO.sub(_iso, text)
    out = _ISO_MONTH.sub(lambda m: f"{_SHORT[int(m.group(2)) - 1]} {m.group(1)}", out)
    return _US.sub(_us, out)
