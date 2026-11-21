"""The simple Ask's figure gate (``operator_api.simple_ask``): every figure a reply states
must be a value a returned ``plx.sql``/``plx.search``/``plx.open`` result holds, or computed
from them (a column's sum or mean, a row count, a subtotal or run of a result's rows, the
sum, difference, ratio or share of two returned values, the change, quotient or product
of two figures its own line states), else it is marked "(unverified)" in place. The reply is never
blanked; its framing (a date, a year, a rank, a table's row number, a code, the session's
own threshold) is no figure."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import bisect
import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

UNVERIFIED = " (unverified)"
# the tools whose results bind a figure (the Ask's read tools; plx.read_document: a
# document read whole, ``operator_api.doc_read_tool``)
TOOLS = ("plx.sql", "plx.search", "plx.open", "plx.read_document")
# results whose numbers bind a reply's figures: the read tools and ``plx.insight`` (a
# built-in tile read as the asker, rows shaped as plx.sql's: operator_api.insight_tool)
INSIGHT = "plx.insight"
EVIDENCE_TOOLS = TOOLS + (INSIGHT,)


# ---------------------------------------------------------------------------- the gate

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
           r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DAY = r"(?:3[01]|[12][0-9]|0?[1-9])"
# Dates and times are the reply's framing (as of, periods), never a figure to bind.
_DATES = re.compile(
    r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?\b"
    r"|\b[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}\b"
    rf"|\b{_MONTHS}\.?\s+[0-9]{{1,2}}(?:st|nd|rd|th)?(?:\s?[-\u2013]\s?[0-9]{{1,2}}"
    rf"(?:st|nd|rd|th)?)?"
    # more days of that month in one list ("Nov 13, 17, 18 and 20"): days 1-31 only, its
    # last after "and", never one a unit or noun follows ("Nov 3, 12 and 95 cases")
    rf"(?:(?:,\s?{_DAY}(?:st|nd|rd|th)?)*,?\s+(?:and|&)\s+{_DAY}"
    rf"(?:st|nd|rd|th)?(?![0-9%]|[.,][0-9])(?=\s*(?:$|[^\sA-Za-z0-9]|\s(?:by|in|on|at|to|"
    rf"from|with|via|for|and|or|but|so|when|while|after|before)\b)))?"
    rf"(?:,?\s+[0-9]{{4}})?\b"
    rf"|\b[0-9]{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b(?:,?\s+[0-9]{{4}})?"
    rf"|\b{_MONTHS}\.?\s+[0-9]{{4}}\b"
    # a weekday and its day ("Mon 9", "Sun, 15"), a day range in a month ("9-15 Nov")
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]{0,6}\.?,?\s+[0-9]{1,2}(?:st|nd|rd|th)?\b"
    rf"|\b[0-9]{{1,2}}(?:st|nd|rd|th)?(?:\s?[-\u2013]\s?|\s+to\s+)[0-9]{{1,2}}(?:st|nd|rd|th)?\s+"
    rf"(?:of\s+)?{_MONTHS}\b"
    r"|\b[0-9]{1,2}:[0-9]{2}(?:\s?[ap]\.?m\.?)?\b"
    r"|\b(?:Q[1-4]|FY|H[12])\s?[0-9]{2,4}\b", re.I)
# A figure as a reply writes it: optional sign and currency sign, digits with thousands
# separators and decimals, then an optional scale word or percent.
_FIGURE = re.compile(
    r"(?<![\w.])(?P<sign>[-−])?(?P<cur>[$€£])?(?P<num>[0-9]{1,3}(?:,[0-9]{3})+"
    r"(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)(?P<scale>\s?(?:[kKmMbB]\b|thousand\b|million\b|"
    r"billion\b|mn\b|bn\b))?(?P<pct>\s?%|\s?percent\b)?(?!(?![xX](?![A-Za-z0-9]))\w)")
# A number that is part of a code (INV-10023, PO #4500123, 10023-A): bound only by the
# same digits in a returned result, never by arithmetic.
_CODE_BEFORE = re.compile(r"(?:[A-Za-z]{1,12}[-/]|#\s?|[A-Za-z][A-Za-z0-9-]*[0-9]\s?[-\u2013/~]\s?)$")
_YEAR_AFTER_WORD = re.compile(r"\b(?:mid|early|late|pre|post|end|start)-$", re.I)
_RUN_BEFORE = re.compile(r"[A-Za-z]+([0-9]{4,})\s?[-\u2013/~]\s?$")
ROUNDED_SHARE = Decimal("0.05")
# words that say a figure is rounded ("about 2,600", "roughly 14,400")
_ROUNDED_BEFORE = re.compile(
    r"\b(?:about|around|roughly|approximately|approx\.?|nearly|almost|some|close\s+to|"
    r"just\s+(?:over|under))\s+(?:[A-Z]{3}\s?)?[$€£]?$|~\s?(?:[A-Z]{3}\s?)?[$€£]?$", re.I)
# a gap of two percentages: "7 points", "1.5 percentage points", "3 pp"
_POINTS_AFTER = re.compile(r"\s?(?:percentage[\s-]+points?|points?|pts|pp)\b", re.I)
# a multiple: "2.2x", "2.2\u00d7", "3 times"
_RATIO_AFTER = re.compile(r"\s?(?:[x\u00d7](?![A-Za-z0-9])|times\b)")
# A plain whole number right after a qualifier is the reply's own criterion or window
# ("at least 3 months", "the top 10", "in the past 30 days"), never a figure of the data.
_CRITERION_BEFORE = re.compile(
    r"\b(?:at\s+least|at\s+most|more\s+than|less\s+than|fewer\s+than|no\s+more\s+than|"
    r"up\s+to|over|under|above|below|top|bottom|last|past|next|first|within|every|"
    r"minimum\s+of|maximum\s+of|each\s+of\s+the)\s+$", re.I)
# a reply's list item: a bullet, a numbered line or a table row
_LIST_ITEM = re.compile(r"^\s*(?:[-*\u2022]\s|[0-9]{1,3}[.)]\s|\|)")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|?\s*$")
_SCALES = {"k": 3, "thousand": 3, "m": 6, "mn": 6, "million": 6, "b": 9, "bn": 9,
           "billion": 9}
_NUMBER_IN_TEXT = re.compile(r"-?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|-?[0-9]+(?:\.[0-9]+)?")
MAX_POOL = 20000      # values kept to bind against (a session reads at most ~40 results)
MAX_PAIR_POOL = 400   # values whose sums, differences and ratios bind
# A result's own subtotals (``_rollups``): the sums of its numeric columns over the rows
# sharing the values of up to ROLLUP_DEPTH of its label columns (GROUPING SETS, as a reply
# totals "CAD before November" from rows by currency, period and type), and the running
# and contiguous sums of the rows in their returned order inside each such group ("Jan-Jun",
# "the top 5"). Contiguous runs only in groups of at most RUN_ROWS rows (of the first
# ROLLUP_LABELS label columns); two values compare (shares, changes, multiples) only in a
# result of at most RUN_ROWS rows, where a reply compares them (``_rollups``).
ROLLUP_DEPTH = 2
ROLLUP_LABELS = 4
RUN_ROWS = 60
# at most this many sums and comparisons from one result (a 200-row result of many
# columns would otherwise take seconds, and the gate runs on every streamed line)
PER_RESULT = 5000
LINE_MOST = 6         # a table row's or a sentence's figures a percentage may compare
ALIGNED_MOST = 4      # rows differing in one label compared pairwise (2025 against 2026)
# a result of at most this many rows compares few enough values that its comparisons bind
# a percentage on any line (a total and its two parts); a longer one's only on a line that
# names the rows or values compared (``_rollups``)
FREE_ROWS = 2
_WHOLE_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9:.+Z-]*)?")
FREE = -1
# a figure precise to the unit or finer (its rounding at most this share of it) may be one
# of the many contiguous sums; a rounded one ("about 782K") only a plain value or subtotal
PRECISE = Decimal("0.00001")
# numeric columns that label a row (a year, a month, a rank) rather than measure it
_LABEL_WORDS = frozenset("""
year yr month mo quarter qtr week wk day dow hour rank bucket k period
""".split())


def _decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text.replace(",", "").replace("−", "-"))
    except (InvalidOperation, ValueError):
        return None


# keys whose values are references and times, never figures
_NOT_FIGURES = frozenset({"result", "cite", "citation", "handle", "as_of", "date", "sent",
                          "received", "seconds", "sql", "views", "columns"})


_MONTHS_HELD = "\u2063months:"   # _strings' line of a text's year-months (_read_text)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield _DATES.sub(" ", value)
        months = _YEAR_MONTH.findall(value)
        if months:   # the year and month each date states (a reply's "2028-05"), whole
            yield _MONTHS_HELD + " ".join(months)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key not in _NOT_FIGURES:
                yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        yield str(value)


def _parsed(result: Any) -> Any:
    """A tool result as the session got it: its JSON (a cut result's text parsed when
    it still parses; else the text)."""
    if isinstance(result, Mapping) and isinstance(result.get("truncated_text"), str):
        try:
            return json.loads(result["truncated_text"])
        except ValueError:
            return result["truncated_text"]
    if isinstance(result, list):   # MCP content blocks
        text = "".join(str(b.get("text") or "") for b in result if isinstance(b, Mapping))
        try:
            return json.loads(text)
        except ValueError:
            return text
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def _cell_number(cell: Any) -> Optional[Decimal]:
    if isinstance(cell, Mapping):
        cell = cell.get("untrusted_text")
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, float, Decimal)):
        return Decimal(str(cell))
    if isinstance(cell, str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cell.strip()):
        return _decimal(cell.strip())
    return None


@dataclass
class Evidence:
    """The values a session's returned results hold (``values``), the few its pairs bind
    from (``headline``: aggregates, small results and their column sums) and the digits of
    identifiers (``digits``: order, invoice and PO numbers, handles; they bind a code the
    reply names, never an amount)."""
    values: list[Decimal] = field(default_factory=list)
    headline: list[Decimal] = field(default_factory=list)
    digits: set[str] = field(default_factory=set)
    results: int = 0
    heads: list[Decimal] = field(default_factory=list)
    # the contiguous sums of a result's rows (``_rollups``): bind a precise figure only
    sums: list[Decimal] = field(default_factory=list)
    # what two values a reply may compare make (``_rollups``), sorted: a percentage binds
    # to one of ``pct`` (a share, its rest, a change, one measure over another), a multiple
    # to one of ``mult``; each with the index of its anchor in ``anchors`` (FREE: a result
    # of at most FREE_ROWS rows, whose few comparisons need no anchor)
    pct: list[tuple[float, int]] = field(default_factory=list)
    mult: list[tuple[float, int]] = field(default_factory=list)
    # (what the line must name: each set's one label at least (``_norm``); the two
    # values): a comparison binds a figure only where the figure's own line names them, or
    # states one of the two values (``_anchored``)
    anchors: list[tuple[tuple, tuple]] = field(default_factory=list)
    # the numbers the session's own calls named (a threshold, a window, a bucket bound):
    # the reply's criteria, never a figure of the data
    criteria: list[Decimal] = field(default_factory=list)
    # the small results' rows (at most CONTRA_ROWS): (the labels a reply may name the row
    # by, its cells), to tell a figure stated for one row that another row holds
    tables: list[tuple[int, list[tuple[tuple[int, int], list]]]] = field(default_factory=list)
    # each result cell's value (to the cent): the results that hold it
    cell_results: dict[Decimal, set[int]] = field(default_factory=dict)
    # a period result's column names (by result), to name the measure a period's row holds
    table_names: dict[int, list[str]] = field(default_factory=dict)

    def finish(self) -> "Evidence":
        self.values = sorted(set(self.values))[:MAX_POOL]
        self.headline = sorted(set(self.headline), key=lambda d: -abs(d))[:MAX_PAIR_POOL]
        self.heads = sorted(set(self.headline) | {abs(d) for d in self.headline})
        self.sums = sorted(set(self.sums) | {abs(d) for d in self.sums})[:MAX_POOL]
        self.criteria = sorted(set(self.criteria))
        self.pct = sorted(set(self.pct))
        self.mult = sorted(set(self.mult))
        return self


# A column holding identifiers (its last word): its cells are codes, never amounts.
_ID_WORDS = frozenset("""
id ids no nos num number numbers code codes handle handles ref refs reference key keys sku
skus lot lots batch po invoice order doc document cite citation item serial account
""".split())
# a token holding a letter and a digit, or a digit run joined by - / # to more: an identifier
_TOKEN = re.compile(r"(?:[^\s,;:()\[\]{}<>\"'|]|,(?=[0-9]{3}))+")
_IDENT = re.compile(r"(?=.*[A-Za-z])(?=.*[0-9])|^#|[0-9][-/#][0-9A-Za-z]")


def _identifier_column(name: Any) -> bool:
    words = re.split(r"[^a-z0-9]+", str(name or "").lower())
    return bool(words) and words[-1] in _ID_WORDS


def _code(ev: Evidence, text: str) -> None:
    for run in re.findall(r"[0-9]+", text):
        ev.digits.add(run)
    ev.digits.add(text.replace(",", "").lstrip("-"))


_CELL_REF = re.compile(r"^[A-Z]{1,3}[0-9]{1,7}=")      # a sheet's cell ("D17=104250")
# a rate's unit ("$6.25/unit", "58.37/kg"): units only, never a month or a code's part
_UNITS = (r"(?:unit|units|ea|each|pc|pcs|piece|kg|g|lb|lbs|oz|l|L|ml|mL|gal|case|cs|cse|"
          r"box|bottle|pail|drum|tote|pallet|hr|hour|day|week|month|year|yr)")
_PER_UNIT = re.compile(rf"(?<=[0-9])/{_UNITS}$")
_SCALED = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,2})?)([kKmM])")
_CUR_PREFIX = re.compile(r"^(?:[A-Z]{1,3}\$|[$\u20ac\u00a3])(?=[0-9])")   # "CN$11.32", "US$5"
_RATE_UNIT = re.compile(rf"/{_UNITS}(?![\w-])")   # a reply's rate: an amount per unit


def _read_text(ev: Evidence, text: str) -> None:
    """Numbers stated in ``text``: amounts unless they sit inside an identifier token
    (a sheet cell's reference and a rate's unit are no part of its number); the year and
    month of every date it states ("2028-05": held whole, never its year's or month's
    digits alone)."""
    if text.startswith(_MONTHS_HELD):
        ev.digits.update(text[len(_MONTHS_HELD):].split())
        return
    for tok in _TOKEN.findall(text):
        tok = _CUR_PREFIX.sub("", _PER_UNIT.sub("", _CELL_REF.sub("", tok.strip(".!?-"))))
        if not tok or not any(ch.isdigit() for ch in tok):
            continue
        if _IDENT.search(tok):
            _code(ev, tok)
            scaled = _SCALED.fullmatch(tok)
            if scaled:   # "3K units", "1.5M": an amount too (a code is never this short)
                ev.values.append(Decimal(scaled.group(1)) * (
                    1000 if scaled.group(2) in "kK" else 1000000))
            continue
        for raw in _NUMBER_IN_TEXT.findall(tok):
            number = _decimal(raw)
            if number is not None:
                ev.values.append(number)
                ev.digits.add(raw.replace(",", "").lstrip("-"))


def _label(cell: Any) -> Optional[str]:
    """A cell as the label it groups rows by (None: empty)."""
    if isinstance(cell, Mapping):
        cell = cell.get("untrusted_text", cell.get("text"))
    return None if cell is None else str(cell)


def _norm(text: Any) -> str:
    """A label or a reply's line as words: lower case, every run of other characters one
    space, padded (``" acme ltd "``) so a label matches whole words only."""
    return " " + " ".join(re.split(r"[^a-z0-9]+", str(text or "").lower())).strip() + " "


# words too common in a label to anchor a comparison on their own
_ANCHOR_SKIP = frozenset("""
the inc ltd llc co corp company limited canada of and a an total all other others none
""".split())


def _label_words(cells: Iterable[Any]) -> frozenset:
    """A row's (or subtotal's) labels as the reply may name them: each whole label and its
    first word of four or more letters ("Fairmont Le Manoir" also as "fairmont")."""
    out = set()
    for cell in cells:
        if cell is None or isinstance(cell, Decimal):
            continue
        whole = _norm(cell)
        if len(whole.strip()) < 2:
            continue
        out.add(whole)
        first = next((w for w in whole.split() if w not in _ANCHOR_SKIP), "")
        if len(first) >= 4:
            out.add(f" {first} ")
    return frozenset(out)


CONTRA_ROWS = 25
_DAY_OF_MONTH = re.compile(rf"\b{_MONTHS}\.?\s+[0-9]{{1,2}}\b|\b[0-9]{{1,2}}\s+{_MONTHS}\b", re.I)
_MONTH_NAMES = ("january february march april may june july august september october "
                "november december").split()
# a period label cell: a month ("2026-10") or a month's first day ("2026-10-01")
_PERIOD_CELL = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])(?:-01(?:[T ]00:00(?::00)?\S*)?)?$")
# a month word as the reply writes a period: capitalised, a full name or its short form
_CAP_MONTH = (r"(?P<m>January|February|March|April|May|June|July|August|September|"
              r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)"
              r"(?![A-Za-z])\.?")
_YEAR = r"(?P<y>(?:19|20)[0-9]{2})(?![0-9])"
# a month acting as a period: with its year ("October 2026"), after in/for/during ("in
# October"), or a sentence's or bullet's own label ("October: 23", "October had 23",
# "October's 23"); "May" the verb, "since mid-September" and "fiscal October" name none
_PERIOD_WITH_YEAR = re.compile(rf"{_CAP_MONTH}\s+{_YEAR}")
_PERIOD_AFTER = re.compile(rf"\b(?:in|for|during)\s+(?:the\s+month\s+of\s+)?{_CAP_MONTH}")
_PERIOD_LABEL = re.compile(rf"^[\s>#*_\-\u2022\d.)]*{_CAP_MONTH}(?P<tail>\**\s*:|'s\b|\s+[a-z])")
# a span, a comparison or a bound: the figure is not stated for the one period named
_SPAN_MONTH = _CAP_MONTH.replace("(?P<m>", "(?:")
_PERIOD_SPAN = re.compile(
    rf"{_SPAN_MONTH}(?:\s+(?:19|20)[0-9]{{2}})?\s*(?:[-\u2013\u2014/]|to|through|and|or)\s*{_SPAN_MONTH}")
_RANGE_WORDS = re.compile(r"\b(?:since|until|till|thru|through|between|before|after|from|"
                          r"vs|versus|compared|than|prior|previous|earlier|later|by)\b", re.I)
_SENTENCE_CUT = re.compile(r"[.;!?](?:\s|$)")


def _period_table(table: Sequence[Sequence[Any]]) -> Optional[list[tuple[tuple[int, int], list]]]:
    """A small result whose rows are periods: one column where every row holds a distinct
    month label ("2026-10" or "2026-10-01"). Its rows keyed by (year, month); any other
    result (first or last dates, a party's rows) is no period table and never judged."""
    if len(table) < 2 or len(table) > CONTRA_ROWS:
        return None
    width = min(len(row) for row in table)
    for col in range(width):
        keys = []
        for row in table:
            m = _PERIOD_CELL.match(row[col]) if isinstance(row[col], str) else None
            if not m:
                break
            keys.append((int(m.group(1)), int(m.group(2))))
        if len(keys) == len(table) and len(set(keys)) == len(keys):
            return [(key, list(row)) for key, row in zip(keys, table)]
    return None


def _month_number(word: str) -> int:
    return next(i for i, n in enumerate(_MONTH_NAMES, 1) if n.startswith(word.lower()[:3]))


def _named_periods(sentence: str) -> Optional[set[tuple[Optional[int], int]]]:
    """The periods a sentence states a figure for, as (year or None, month); None when it
    spans, compares or bounds periods (no one period is the figure's)."""
    if _PERIOD_SPAN.search(sentence) or _RANGE_WORDS.search(sentence):
        return None
    text = _DAY_OF_MONTH.sub(lambda m: " " * len(m.group(0)), sentence)  # a day: a date
    out: set[tuple[Optional[int], int]] = set()
    years = {m.start("m"): int(m.group("y")) for m in _PERIOD_WITH_YEAR.finditer(text)}
    for start in years:
        out.add((years[start], _month_number(text[start:start + 3])))
    for m in _PERIOD_AFTER.finditer(text):
        if m.start("m") not in years:
            out.add((None, _month_number(m.group("m"))))
    label = _PERIOD_LABEL.match(text)
    if label and label.start("m") not in years and not (
            label.group("m") == "May" and not label.group("tail").rstrip().endswith(":")):
        out.add((None, _month_number(label.group("m"))))
    return out


# where a sentence naming two periods splits into clauses, each stating its own period's
# figures ("23 orders in October and 5 in November", "October: 23 orders, 18 in September")
_CLAUSE_CUT = re.compile(r"[,;]\s+|\s+(?:and|but|while|whereas)\s+")


def contradicts(fig: "Figure", line: str, ev: Evidence, at: Optional[int] = None) -> bool:
    """Whether ``fig``, stated on ``line`` for exactly one period of a small period result
    ("23 orders in October"), is that result's value for another period while the named
    period's row holds another value in that column and none equal to it (the October row
    says 5, November's 23). A sentence naming two periods is judged clause by clause
    ("23 orders in October and 23 in November"). Not judged: a sentence or clause naming
    no period or two, a span or a comparison; a figure some other result holds; a table
    row."""
    return bool(_contradiction(fig, line, ev, at))


def _contradiction(fig: "Figure", line: str, ev: Evidence,
                   at: Optional[int] = None, notes: Optional[list] = None) -> Optional[set]:
    """The named period's own values in the columns where another period's row holds
    ``fig`` (``contradicts``); None or empty: no contradiction. ``notes``: gets the line
    saying the named period's own value of the measure the figure names (``_period_note``)
    when the word after the figure names exactly one of the result's columns."""
    if fig.code or fig.percent or fig.ratio or line.lstrip().startswith("|"):
        return None   # a table row lists rows side by side: judged as a sentence only
    if fig.plain and abs(fig.value) < 10:
        return None   # a small whole number (a rank, one of a few) names nothing alone
    at = line.find(fig.text) if at is None else at
    if at < 0:
        return None
    after = line[at + len(fig.text):at + len(fig.text) + 2]
    before = line[max(0, at - 2):at]
    if re.match(r"\s?[-\u2013\u2014]\s?[0-9]", after) or re.search(r"[-\u2013\u2014]\s?$", before):
        return None   # a range's bound ("290-335"), not a period's figure
    # the sentence holding the figure: its whole clause and bullet label, never cut at a
    # colon or a dash ("October: 23 orders, up from 18 in September" names two periods)
    cuts = [m.end() for m in _SENTENCE_CUT.finditer(line)]
    begin = max([c for c in cuts if c <= at] or [0])
    finish = min([c for c in cuts if c > at] or [len(line)])
    periods = _named_periods(line[begin:finish])
    if periods and len(periods) > 1:
        # two periods in one sentence: the clause holding the figure, when it names one
        sentence = line[begin:finish]
        marks = [0] + [m.end() for m in _CLAUSE_CUT.finditer(sentence)] + [len(sentence) + 1]
        rel = at - begin
        lo = max(c for c in marks if c <= rel)
        hi = min([m.start() for m in _CLAUSE_CUT.finditer(sentence) if m.start() >= rel]
                 or [len(sentence)])
        periods = _named_periods(sentence[lo:hi])
    if not periods or len(periods) != 1:
        return None
    (year, month), = periods
    target = abs(fig.value)

    def near(cell: Any) -> bool:
        return isinstance(cell, Decimal) and abs(abs(cell) - target) <= max(
            fig.tol, Decimal("0.005"))
    found: set = set()
    for index, table in ev.tables:
        named = [i for i, ((y, m), _) in enumerate(table)
                 if m == month and (year is None or y == year)]
        if len(named) != 1:
            continue
        own = table[named[0]][1]
        if any(near(c) for c in own):
            return None   # the named period's row holds it
        for i, (_, cells) in enumerate(table):
            if i == named[0]:
                continue
            for col, cell in enumerate(cells):
                if near(cell) and col < len(own) and isinstance(own[col], Decimal):
                    if ev.cell_results.get(_cell_key(target), {index}) - {index}:
                        return None   # another result holds it: stated from there
                    found.add(own[col])
    if found and notes is not None:
        said = _period_note(fig, line, at, ev, year, month)
        if said and said not in notes:
            notes.append(said)
    return found


_MEASURE_AFTER = re.compile(r"\s*(?:[A-Z]{3}\s+)?([A-Za-z][A-Za-z-]{2,})")


def _singular(word: str) -> str:
    word = word.lower()
    return word[:-3] + "y" if word.endswith("ies") else word[:-1] if word.endswith("s") \
        and not word.endswith("ss") else word


def _period_note(fig: "Figure", line: str, at: int, ev: Evidence, year: Optional[int],
                 month: int) -> Optional[str]:
    """"The records show 5 orders for October 2026.": the named period's own value of the
    measure the word after ``fig`` names ("23 orders" -> a column named orders or
    order_count), when exactly one column of exactly one period result matches it and that
    period's row holds a number there; else None (the figure is only marked)."""
    m = _MEASURE_AFTER.match(line, at + len(fig.text))
    if not m:
        return None
    word = _singular(m.group(1))
    hits = []
    for index, table in ev.tables:
        names = ev.table_names.get(index) or []
        cols = [i for i, n in enumerate(names)
                if word in [_singular(w) for w in re.split(r"[^A-Za-z]+", n) if w]]
        named = [cells for (y, mo), cells in table if mo == month and (year is None or y == year)]
        if len(cols) == 1 and len(named) == 1 and cols[0] < len(named[0]) \
                and isinstance(named[0][cols[0]], Decimal):
            y = next(y for (y, mo), _ in table if mo == month and (year is None or y == year))
            hits.append((named[0][cols[0]], y))
    if len({h[0] for h in hits}) != 1:
        return None
    value, y = hits[0]
    shown = f"{value:,}" if value == value.to_integral_value() and abs(value) >= 1000 \
        else (f"{value:,.2f}" if value != value.to_integral_value() else f"{value}")
    return f"The records show {shown} {m.group(1)} for {_MONTH_NAMES[month - 1].title()} {y}."


def _cell_key(value: Decimal) -> Decimal:
    return abs(value).quantize(Decimal("0.01")) if abs(value) < 10 ** 15 else abs(value)


def _rollups(ev: Evidence, names: Sequence[Any], table: Sequence[Sequence[Any]]) -> None:
    """A result's own subtotals, running sums and comparisons (``ROLLUP_DEPTH``): what a
    reply adds up or compares from the rows it read (the CAD rows before November, January
    to June, the top 5, this October against last) binds as that, never as a figure from
    nowhere. Numeric columns are summed; the others (text, codes, and numbers that label a
    row: a year, a month, a rank) group. Two values compare only as a reply frames them:
    a row's value as a share of its group's total in the same column ("Acme was 23% of
    revenue", "81% of the USD total", or the rest's share; named by the row or the
    group), two measures of one row (margin over revenue, this
    year's column against last year's), and one column's values on two rows or subtotals
    whose labels differ in exactly one place (CAD 2026 against CAD 2025: the change). Each
    comparison is anchored to the labels of the rows it compares and its two values: it
    binds a figure only on a line of the reply that names one of them (``bound``), so a
    result's many comparisons are no pool any percentage falls into; a result of at most
    FREE_ROWS rows compares too few to need the anchor."""
    from itertools import combinations
    width = max((len(r) for r in table), default=0)

    def at(row: Sequence[Any], i: int) -> Any:
        return row[i] if i < len(row) else None

    def labels_row(i: int) -> bool:
        words = re.split(r"[^a-z0-9]+", str(names[i] if i < len(names) else "").lower())
        return bool(words) and words[-1] in _LABEL_WORDS

    numeric = [i for i in range(width)
               if any(isinstance(at(r, i), Decimal) for r in table)
               and all(at(r, i) is None or isinstance(at(r, i), Decimal) for r in table)
               and not labels_row(i)]
    # a column of whole dates measures a row (its last invoice, its due date) unless its
    # name says it labels one (a day, a week): it groups, but is never part of the key two
    # rows are aligned on
    dated = [i for i in range(width) if i not in numeric and not labels_row(i)
             and any(at(r, i) is not None for r in table)
             and all(at(r, i) is None or _WHOLE_DATE.fullmatch(str(at(r, i)))
                     for r in table)]
    labels = [i for i in range(width) if i not in numeric][:ROLLUP_LABELS]
    pct: list[tuple[float, int]] = []
    mult: list[tuple[float, int]] = []
    sums: list[Decimal] = []

    summary = len(table) <= RUN_ROWS   # a long listing's rows are no comparison
    free = len(table) <= FREE_ROWS

    def anchor(need: tuple, a: Decimal, b: Decimal) -> int:
        if free:
            return FREE
        ev.anchors.append((tuple(w for w in need if w), (abs(a), abs(b))))
        return len(ev.anchors) - 1

    def compare(kind: str, a: Any, b: Any, need: tuple) -> None:
        """``kind`` share: ``b`` is a part of the total ``a`` (its share, the rest's);
        measure: two columns of one row (each over the other, as is and as a change);
        change: one column on two rows (the change either way, and as a share)."""
        if not (summary and isinstance(a, Decimal) and isinstance(b, Decimal) and a and b
                and len(pct) < PER_RESULT):
            return
        at_ = anchor(need, a, b)
        r = abs(float(b) / float(a))
        if kind == "share":
            pct.extend(((r, at_), (abs(1 - r), at_)))
            return
        for q in (r, 1 / r):
            pct.extend(((q, at_), (abs(q - 1), at_)))
            mult.append((q, at_))

    def row_words(n: int, skip: Sequence[int] = ()) -> frozenset:
        return _label_words(at(table[n], i) for i in labels if i not in skip)

    def key_words(key: Sequence[Any]) -> frozenset:
        # a group's key as the line names it ("of CAD revenue", "Acme's")
        return _label_words(key)

    def aligned(keyed: Sequence[tuple[tuple, Any]]) -> None:
        # pairs whose keys differ in exactly one place (this month against last year's)
        size = len(keyed[0][0]) if keyed else 0
        for j in range(size):
            buckets: dict[tuple, list[tuple[tuple, Any]]] = {}
            for key, value in keyed:
                buckets.setdefault(key[:j] + key[j + 1:], []).append((key, value))
            for pairs in buckets.values():
                if 1 < len(pairs) <= ALIGNED_MOST:
                    for (kx, x), (ky, y) in combinations(pairs, 2):
                        # named by what both share ("CAD is down 2.9% on last year",
                        # "Acme's revenue rose 12%"), else by either side
                        same = key_words(kx[:j] + kx[j + 1:])
                        compare("change", x, y, (same or key_words((kx[j], ky[j])),))

    for n, row in enumerate(table):   # two measures of one row
        for x, y in combinations([at(row, c) for c in numeric], 2):
            compare("measure", x, y, (row_words(n),))
    for depth in range(0, min(ROLLUP_DEPTH, len(labels)) + 1):
        for subset in combinations(labels, depth):
            groups: dict[tuple, list[int]] = {}
            for n, row in enumerate(table):
                groups.setdefault(tuple(at(row, i) for i in subset), []).append(n)
            if depth and len(groups) == len(table):
                continue   # each row its own group: the rows themselves
            for c in numeric:
                keyed: list[tuple[tuple, Any]] = []
                for key, idx in groups.items():
                    held = [(n, at(table[n], c)) for n in idx
                            if isinstance(at(table[n], c), Decimal)]
                    vals = [v for _, v in held]
                    if not vals:
                        continue
                    total = sum(vals, Decimal(0))
                    keyed.append((key, total))
                    if len(vals) < 2:
                        continue
                    ev.values.append(total)
                    for n, v in held:   # a row's share of its group (named by the row)
                        compare("share", total, v, (row_words(n, subset) | key_words(key),))
                    running = Decimal(0)
                    for k, v in enumerate(vals):
                        running += v
                        if k:
                            sums.append(running)
                    if len(vals) <= RUN_ROWS and depth <= 1 and len(sums) < PER_RESULT:
                        for x in range(1, len(vals) - 1):
                            run = vals[x]
                            for y in range(x + 1, len(vals)):
                                run += vals[y]
                                sums.append(run)
                if depth == len(labels):
                    continue   # the rows themselves: compared below
                if depth:   # subtotals: CAD 2025 against CAD 2026
                    aligned([(tuple(k for i, k in zip(subset, key) if i not in dated), v)
                             for key, v in keyed])
    for c in numeric:   # rows whose labels differ in one place
        aligned([(tuple(at(r, i) for i in labels if i not in dated), at(r, c))
                 for r in table])
    ev.sums += sums[:PER_RESULT]
    ev.pct += pct
    ev.mult += mult


def _criteria(ev: Evidence, arguments: Any) -> None:
    """The numbers a call's own arguments name (a statement's thresholds and windows)."""
    for text in _arg_strings(arguments):
        for raw in _NUMBER_IN_TEXT.findall(_DATES.sub(" ", text)):
            number = _decimal(raw)
            if number is not None:
                ev.criteria.append(abs(number))


def _arg_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _arg_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _arg_strings(item)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        yield str(value)


def evidence(events: Sequence[Mapping[str, Any]],
             tools: Sequence[str] = EVIDENCE_TOOLS) -> Evidence:
    """Every number a returned ``plx.sql``, ``plx.insight`` or ``plx.search`` result holds, with each SQL
    result's row count and numeric columns' sums and means. Cells of identifier columns
    and identifier tokens in text (INV-20417, SO-1083) are codes: they bind a code the
    reply names, never an amount or an addend. A refused or failed call returns nothing
    to bind."""
    ev = Evidence()
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error"):
            continue
        name = str(event.get("name") or "")
        if name not in tools:
            continue
        result = _parsed(event.get("result"))
        if result is None or (isinstance(result, Mapping) and result.get("error")):
            continue
        ev.results += 1
        _criteria(ev, event.get("arguments"))
        rows = result.get("rows") if isinstance(result, Mapping) else None
        rest = ({k: v for k, v in result.items() if k != "rows"}
                if isinstance(rows, list) else result)
        for text in _strings(rest):
            _read_text(ev, text)
        if isinstance(rows, list):
            names = result.get("columns") if isinstance(result.get("columns"), list) else []
            ids = {i for i, c in enumerate(names) if _identifier_column(c)}
            ev.values.append(Decimal(len(rows)))
            ev.headline.append(Decimal(len(rows)))
            columns: dict[int, list[Decimal]] = {}
            table: list[list[Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                for text in _strings({k: v for k, v in row.items() if k != "cells"}):
                    _read_text(ev, text)
                cells = row.get("cells")
                line: list[Any] = []
                for i, cell in enumerate(cells if isinstance(cells, list) else ()):
                    if i in ids:
                        for text in _strings(cell):
                            _code(ev, text)
                        line.append(_label(cell))
                        continue
                    for text in _strings(cell):
                        _read_text(ev, text)
                    number = _cell_number(cell)
                    line.append(_label(cell) if number is None else number)
                    if number is not None:
                        ev.cell_results.setdefault(_cell_key(number), set()).add(ev.results)
                        columns.setdefault(i, []).append(number)
                        if len(rows) <= 25:
                            ev.headline.append(number)
                table.append(line)
            periods = _period_table(table)
            if periods:
                ev.tables.append((ev.results, periods))
                ev.table_names[ev.results] = [str(n) for n in names]
            for numbers in columns.values():
                total = sum(numbers, Decimal(0))
                ev.values += [total, total / len(numbers)]
                ev.headline += [total, Decimal(len(numbers))]
            _rollups(ev, names, table)
        hits = result.get("documents") if isinstance(result, Mapping) else None
        if isinstance(hits, list):
            ev.values.append(Decimal(len(hits)))
    return ev.finish()


def _near(pool: Sequence[Decimal], target: Decimal, tol: Decimal) -> bool:
    i = bisect.bisect_left(pool, target - tol)
    return i < len(pool) and pool[i] <= target + tol


@dataclass
class Figure:
    start: int
    end: int
    text: str
    value: Decimal
    tol: Decimal
    percent: bool
    code: bool = False
    plain: bool = False       # a whole number with no currency, separator, scale or %
    ratio: bool = False       # a multiple ("2.2x", "3 times")
    rounded: bool = False     # said as approximate ("about 2,600"): bound to its rounding
    expanded: Optional[str] = None   # a code run's end ("...783\u2013784"): its whole code


def figures(text: str) -> list[Figure]:
    """The figures a reply states (dates, times, years and list numbers left out)."""
    masked = _DATES.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in _FIGURE.finditer(masked):
        num = m.group("num")
        value = _decimal(num)
        if value is None:
            continue
        code = bool(_CODE_BEFORE.search(masked[max(0, m.start() - 14):m.start()])) or (
            masked[m.end():m.end() + 1] in ("-", "/") and masked[m.end() + 1:m.end() + 2]
            .isalnum() and not _RATE_UNIT.match(masked, m.end()))   # "1,171.40/kg": a rate
        if code:
            before = masked[max(0, m.start() - 24):m.start()]
            if before.rstrip().endswith("#") and re.fullmatch(r"[0-9]{1,2}", num):
                continue   # a rank or position ("the #5 customer"), never a figure
            if _YEAR_AFTER_WORD.search(before) and re.fullmatch(r"(?:19|20)[0-9]{2}", num):
                continue   # a year ("mid-2020", "pre-2019")
            run = _RUN_BEFORE.search(before)
            # the end of a run of codes ("IN1000180783\u2013784"): the code it stands for
            whole = (run.group(1)[:max(0, len(run.group(1)) - len(num))] + num) if run \
                and len(num) < len(run.group(1)) else None
            out.append(Figure(m.start(), m.end(), text[m.start():m.end()], value,
                              Decimal(0), False, True,
                              expanded=whole or num.replace(",", "")))
            continue
        if re.fullmatch(r"0[1-9]|1[0-2]", num) and re.search(
                r"(?<![\w-])(?:19|20)[0-9]{2}-$", masked[max(0, m.start() - 6):m.start()]):
            continue   # the month of a year and month ("2028-05"), judged with its year
        scale = (m.group("scale") or "").strip().lower()
        pct = bool(m.group("pct"))
        plain = "," not in num and "." not in num and not m.group("cur")
        rounded = bool(_ROUNDED_BEFORE.search(masked[max(0, m.start() - 24):m.start()]))
        ratio = bool(_RATIO_AFTER.match(masked, m.end()))
        if plain and not scale and not pct:
            if 1900 <= value <= 2100 and len(num) == 4:
                continue   # a year
            line_start = masked.rfind("\n", 0, m.start()) + 1
            if not masked[line_start:m.start()].strip() and masked[m.end():m.end() + 2] in (
                    ". ", ") "):
                continue   # a list number
            if masked[m.start() - 1:m.start()] in ('"', "\u201c") and \
                    masked[m.end():m.end() + 2] in (". ", ") "):
                continue   # a quoted text's clause number ('Before: "2. Payment terms')
            if _CRITERION_BEFORE.search(masked[max(0, m.start() - 24):m.start()]):
                continue   # the reply's own criterion or window
        places = len(num.split(".", 1)[1]) if "." in num else 0
        tol = Decimal(5) * Decimal(10) ** (-places - 1)
        whole = num.replace(",", "")
        zeros = len(whole) - len(whole.rstrip("0")) if not places else 0
        if zeros and (rounded or zeros >= 3):
            # "about 2,600": the nearest hundred; "152,000": a whole thousand is read as
            # rounded to the thousand; never looser than ROUNDED_SHARE of the figure
            tol = max(tol, min(Decimal(5) * Decimal(10) ** (zeros - 1),
                               Decimal(whole) * ROUNDED_SHARE))
        exp = _SCALES.get(scale, 0)
        if exp:
            value, tol = value * Decimal(10) ** exp, tol * Decimal(10) ** exp
        before = text[m.start() - 1] if m.start() else ""
        if m.group("sign") and not (before.isdigit() or before.isalpha()):
            value = -value   # (a range's "1-31" is no negative 31, "mid-2020" no -2020)
        elif m.group("sign") and plain and not scale and not pct \
                and 1900 <= value <= 2100 and len(num) == 4:
            continue   # a year after a hyphen ("mid-2020")
        out.append(Figure(m.start(), m.end(), text[m.start():m.end()], value, tol, pct,
                          plain=plain and not scale and not pct and not ratio,
                          ratio=ratio and not pct, rounded=rounded))
    return out


def bound(fig: Figure, ev: Evidence, line: str = "",
          beside: Sequence[Figure] = ()) -> bool:
    """Whether ``fig`` is a returned value or computed from returned values. ``line``: the
    reply's line holding it and ``beside``: the other figures that line states; a
    percentage or multiple binds to a comparison of two returned values only where they
    name the rows or one of the values compared (``_rollups``)."""
    if fig.code:
        return any(key in ev.digits for key in (
            fig.expanded, fig.text.replace(",", "").lstrip("-\u2212$")) if key)
    if re.fullmatch(r"[0-9]+[kKmMbB]", fig.text) and fig.text in ev.digits:
        return True   # a scale-lettered token a result holds as a code ("lot 26127B")
    pool = ev.values
    for target in {fig.value, abs(fig.value)}:
        if _near(pool, target, fig.tol):
            return True
    if fig.plain and fig.text.lstrip("-\u2212") in ev.digits:
        return True   # a returned identifier named bare ("PO 106017"), whole
    if not fig.percent and not fig.ratio:
        if fig.tol <= abs(fig.value) * PRECISE and _near(
                ev.sums, abs(fig.value), fig.tol):
            return True   # a contiguous sum of a result's rows (January to June, top 5)
        if fig.tol >= Decimal("0.5") and _near(ev.criteria, abs(fig.value), fig.tol):
            return True   # the session's own threshold or window ("under 50,000")
    if fig.percent or fig.ratio:
        scale = 100.0 if fig.percent else 1.0
        # a share, change or multiple of two values the line compares (``_rollups``)
        want, spread = float(abs(fig.value)) / scale, float(fig.tol) / scale
        pool = ev.pct if fig.percent else ev.mult
        words = _norm(line)
        stated = [f for f in beside if not f.code and not f.percent and not f.ratio]
        seen: set[int] = set()
        i = bisect.bisect_left(pool, (want - spread, FREE - 1))
        while i < len(pool) and pool[i][0] <= want + spread:
            at_ = pool[i][1]
            i += 1
            if at_ in seen:
                continue
            seen.add(at_)
            if at_ == FREE or _anchored(ev.anchors[at_], words, stated):
                return True
        return False
    target = abs(fig.value)
    # a + b or a - b of two returned values
    # (the other value must matter: a figure within its own rounding of one value is
    # that value, never that value plus some small number found anywhere)
    # (both headline values: a sum of a headline and any value found anywhere binds
    # nearly every number, a wrong total included)
    return any(abs(target - a) > 2 * fig.tol and (
        _near(ev.heads, target - a, fig.tol) or _near(ev.heads, a - target, fig.tol)
        or _near(ev.heads, a + target, fig.tol)) for a in ev.headline)


def _anchored(anchor: tuple[tuple, tuple], words: str,
              stated: Sequence[Figure]) -> bool:
    """Whether a reply's line (``words``: ``_norm``) names what a comparison compares (a
    label of each set the anchor needs) or states one of the two values compared."""
    need, values = anchor
    if need and all(any(label in words for label in labels) for labels in need):
        return True
    return any(abs(abs(f.value) - v) <= max(f.tol, Decimal("0.005")) for f in stated
               for v in values)


def list_counts(text: str) -> set[Decimal]:
    """How many items each list (bullets, numbered lines, table rows) of a reply holds."""
    counts, run, table = set(), 0, False
    for line in text.splitlines() + [""]:
        if _TABLE_RULE.match(line):
            continue
        if _LIST_ITEM.match(line):
            run += 1
            table = table or line.lstrip().startswith("|")
            continue
        if run:
            counts.add(Decimal(run))
            if table:
                counts.add(Decimal(run - 1))   # a table's header row
        run, table = 0, False
    return {c for c in counts if c > 0}


def _compares_line(fig: Figure, stated: Sequence[Figure], text: str) -> bool:
    """Whether a percentage, multiple or decimal is the share, change, multiple or
    quotient of two other figures stated on its own line (a table row's "411,106.19 |
    522,285.50 | +27.0%", a unit price "24,192 | 11,781.50 | 0.487")."""
    start = text.rfind("\n", 0, fig.start) + 1
    end = text.find("\n", fig.end)
    end = len(text) if end < 0 else end
    values = [float(abs(f.value)) for f in stated if start <= f.start < end and f is not fig
              and not f.code and not f.percent and not f.ratio and f.value]
    if len(values) > LINE_MOST:
        return False   # a line of many figures compares nothing in particular
    want, tol = float(abs(fig.value)), float(fig.tol)
    scale = 100.0 if fig.percent else 1.0
    for i, a in enumerate(values):
        for j, b in enumerate(values):
            if i == j:
                continue
            r = b / a
            if abs(r * scale - want) <= tol or (fig.percent and abs(
                    abs(r - 1) * scale - want) <= tol):
                return True
    return False


def _product_of_line(fig: Figure, factors: Sequence[Figure], text: str) -> bool:
    """Whether an amount is the product of two other figures stated on its own line (a
    line's "12 x 20L @ 210.00 CAD = 2,520.00", a table row's "500 kg | 456.89 CAD/kg |
    228,445.00", or a percent of a stated amount: "228,445.00 | 8% | 18,275.60"). Only
    ``factors`` count: the line's figures a result (or the person) binds on their own,
    never one bound only by another product or comparison of the line; neither may be the
    figure's own value."""
    if fig.code or fig.percent or fig.ratio or not fig.value:
        return False
    start = text.rfind("\n", 0, fig.start) + 1
    end = text.find("\n", fig.end)
    end = len(text) if end < 0 else end
    values = [float(abs(f.value)) / (100.0 if f.percent else 1.0) for f in factors
              if start <= f.start < end and f is not fig and not f.code and f.value]
    if len(values) > LINE_MOST:
        return False   # a line of many figures multiplies to nearly anything
    want, tol = float(abs(fig.value)), float(fig.tol)
    for i, a in enumerate(values):
        for b in values[i + 1:]:
            if min(a, b) == 1.0 or abs(a - want) <= tol or abs(b - want) <= tol:
                continue   # x 1 or one factor the figure itself: no computation
            if abs(a * b - want) <= tol:
                return True
    return False


def _row_number(text: str, at: int) -> int:
    """The body row of a Markdown table the line holding ``at`` is (1 = the first under
    the header rule); 0 when it is no table body row."""
    lines = text[:at].split("\n")[:-1]
    n = 1
    for line in reversed(lines):
        if _TABLE_RULE.match(line):
            return n
        if not line.lstrip().startswith("|"):
            return 0
        n += 1
    return 0


_CLAUSE_NUMBER = re.compile(r"(?m)^[ \t>*#-]*([0-9]{1,3}(?:\.[0-9]{1,3})?)(?:[.)]\s|\s(?=[A-Z]))")
_YEAR_MONTH = re.compile(r"(?<![\w-])((?:19|20)[0-9]{2}-(?:0[1-9]|1[0-2]))(?![0-9])")


def _year_month(fig: "Figure", text: str) -> Optional[str]:
    """The year and month a figure starts ("2028" of "2028-05"), else None."""
    m = _YEAR_MONTH.match(text, fig.start)
    return m.group(1) if m and fig.end == fig.start + 4 else None


_CLAUSE_WORD = re.compile(r"\b(?:clause|section|article|paragraph|schedule|\u00a7)\s?$", re.I)


def _clause_quoted(fig: "Figure", text: str) -> bool:
    """Whether a figure is written as a clause number: "2. Payment terms", "2) ...",
    "Clause 2", "section 2"."""
    return bool(re.match(r"[.)]\s+[A-Z]", text[fig.end:fig.end + 4])
                or _CLAUSE_WORD.search(text[max(0, fig.start - 12):fig.start]))


def gate(text: str, events: Sequence[Mapping[str, Any]], asked: Sequence[str] = (),
         ev: Optional[Evidence] = None, files: Sequence[str] = ()) -> tuple[str, list[str]]:
    """The reply with every unbound figure marked "(unverified)" right after it, and the
    figures marked. A figure the person's own words state (the question, the earlier
    turns) needs no binding. Never removes a word. ``ev``: ``evidence(events)`` when the
    caller already holds it; ``files``: the text of the person's attached files (their
    clause numbers and year-months quoted back need no binding)."""
    ev = evidence(events) if ev is None else ev
    own = {f.value for said in asked for f in figures(said)} | {
        abs(f.value) for said in asked for f in figures(said)}
    # the clause and list numbers that start a line of the person's own files ("2.",
    # "2)", "2.3 "): a reply quoting "2. Payment terms" of the attached contract quotes
    # it; never a date's parts, nor a number of the question or an earlier answer
    clauses = {n for said in files for raw in _CLAUSE_NUMBER.findall(str(said or ""))
               if (n := _decimal(raw)) is not None}
    # a year and month ("best before 2028-05") is held only when a result or one of the
    # person's files states that year and month
    months = {m for said in files for m in _YEAR_MONTH.findall(str(said or ""))}
    items = list_counts(text)
    marked: list[str] = []
    out, last = [], 0
    stated = figures(text)
    ok: set[int] = set()      # the figures (by position) a result or the person binds
    held: list[Figure] = []   # those, and a multiplier ("12 x") whose number a result holds
    for fig in stated:
        start = text.rfind("\n", 0, fig.start) + 1
        end = text.find("\n", fig.end)
        end = len(text) if end < 0 else end
        beside = [f for f in stated if start <= f.start < end and f is not fig]
        month = _year_month(fig, text)
        if month is not None:
            if month in months or month in ev.digits:
                ok.add(fig.start)
                held.append(fig)
            continue
        if fig.value in own or abs(fig.value) in own or (
                fig.plain and fig.value in clauses and _clause_quoted(fig, text)) or bound(
                fig, ev, text[start:end], beside):
            ok.add(fig.start)
            held.append(fig)
        elif fig.ratio and _near(ev.values, abs(fig.value), fig.tol):
            held.append(fig)
    # a figure bound only as another row's value of the row its line names: wrong row
    contra: set[int] = set()
    notes: list[str] = []   # the named period's own value, said once under the reply
    for fig in stated:
        if fig.start in ok and fig.value not in own and abs(fig.value) not in own:
            start = text.rfind("\n", 0, fig.start) + 1
            end = text.find("\n", fig.end)
            line = text[start:len(text) if end < 0 else end]
            if _contradiction(fig, line, ev, fig.start - start, notes):
                ok.discard(fig.start)
                held = [f for f in held if f.start != fig.start]
                contra.add(fig.start)
    # a product of two held figures is held in turn (a line's amount, then its share of
    # tax): every step needs two figures held before it, so nothing holds without a result
    grown = True
    while grown:
        grown = False
        for fig in stated:
            if fig.start not in ok and _product_of_line(fig, held, text):
                ok.add(fig.start)
                held.append(fig)
                grown = True
    for fig in stated:
        if fig.start in ok:
            continue
        if fig.start in contra:
            tag = _tag_at(text, fig)
            out.append(text[last:tag])
            out.append(UNVERIFIED)
            last = tag
            marked.append(fig.text)
            continue
        if _of_reply(fig, held):
            continue   # the sum or difference of two figures the reply holds
        if (fig.percent or fig.ratio or "." in fig.text) and _compares_line(fig, stated, text):
            continue   # the change, share or quotient of two figures its own line states
        if _POINTS_AFTER.match(text, fig.end) and any(
                abs(abs(a.value - b.value) - abs(fig.value)) <= max(fig.tol, a.tol + b.tol)
                for a in held for b in held if a.percent and b.percent and a is not b):
            continue   # "7 points below": the gap of two held percentages the reply states
        line = text[text.rfind("\n", 0, fig.start) + 1:fig.start]
        if fig.plain and fig.value in items and not _LIST_ITEM.match(line):
            continue   # how many items the reply itself lists (never a cell of a list)
        if fig.plain and re.fullmatch(r"\s*\|\s*", line) \
                and fig.value == _row_number(text, fig.start):
            continue   # a table's own row number ("| 7 | K18 Inc |"), never a figure
        tag = _tag_at(text, fig)
        out.append(text[last:tag])
        out.append(UNVERIFIED)
        last = tag
        marked.append(fig.text)
    out.append(text[last:])
    if notes:   # a wrong period's figure stays marked; the period's own value beside it
        out.append("\n\n" + " ".join(notes))
    return "".join(out), marked


# what a figure's tag goes after: its currency code or unit ("9,355.66 CAD", "12 kg"),
# never between the number and what it counts
_UNIT_AFTER = re.compile(r"(?:\s?(?:[A-Z]{3}|kg|g|lb|lbs|L|ml|EA|ea|pcs)(?![A-Za-z0-9]))?")


def _tag_at(text: str, fig: Figure) -> int:
    """Where the "(unverified)" tag of ``fig`` goes: after the figure and its currency
    code or unit, so the tag never splits a figure from its unit."""
    m = _UNIT_AFTER.match(text, fig.end)
    return m.end() if m else fig.end


# a reply's own arithmetic binds only figures precise enough that a chance match is
# unlikely, and only over a reply of at most this many held figures
REPLY_ARITH_MOST = 60


def _of_reply(fig: Figure, held: Sequence[Figure]) -> bool:
    """Whether ``fig`` is the sum or difference of two figures the reply states that a
    result binds (``held``): a change row's "-9,355.66" under 222,570.00 and 213,214.34,
    "almost 62%" of 37.4% and 24.4%. Amounts only when precise (to the cent or 5
    significant digits); percentages only of two held percentages."""
    if fig.code or fig.ratio or not fig.value:
        return False
    pool = [f for f in held if not f.code and not f.ratio and f.percent == fig.percent
            and f is not fig]
    if len(pool) > REPLY_ARITH_MOST or len(pool) < 2:
        return False
    if not fig.percent and fig.tol * 2 > abs(fig.value) * Decimal("0.0001"):
        return False
    want = abs(fig.value)
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            x, y = abs(a.value), abs(b.value)
            if abs(x - want) <= fig.tol or abs(y - want) <= fig.tol:
                continue   # one of them is the figure itself: no computation
            slack = fig.tol + a.tol + b.tol
            if abs(x + y - want) <= slack or abs(abs(x - y) - want) <= slack:
                if fig.percent and x + y > 100 + slack:
                    continue
                return True
            if not fig.percent and min(x, y) >= 1:
                # an average the reply works out: an amount over a count it states
                # ("about 44,514 an order" of 222,570.00 over 5 orders)
                (big, bt), (small, st) = sorted(((x, a), (y, b)), key=lambda p: -p[0])
                # a count (a plain whole number) is exact; a rounded divisor widens it
                spread = bt.tol / small + (Decimal(0) if st.plain else big * st.tol / (small * small))
                if abs(big / small - want) <= fig.tol + spread:
                    return True
    return False
