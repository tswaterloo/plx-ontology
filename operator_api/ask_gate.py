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
# Dates and times are the reply's framing (as of, periods), never a figure to bind.
_DATES = re.compile(
    r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?\b"
    r"|\b[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}\b"
    rf"|\b{_MONTHS}\.?\s+[0-9]{{1,2}}(?:st|nd|rd|th)?(?:\s?[-\u2013]\s?[0-9]{{1,2}}"
    rf"(?:st|nd|rd|th)?)?"
    # more days of that month in one list ("Nov 13, 17, 18 and 20"): its last after "and"
    rf"(?:(?:,\s?[0-9]{{1,2}}(?:st|nd|rd|th)?)*,?\s+(?:and|&)\s+[0-9]{{1,2}}"
    rf"(?:st|nd|rd|th)?(?![0-9%]|[.,][0-9]))?(?:,?\s+[0-9]{{4}})?\b"
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


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield _DATES.sub(" ", value)
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


def _read_text(ev: Evidence, text: str) -> None:
    """Numbers stated in ``text``: amounts unless they sit inside an identifier token."""
    for tok in _TOKEN.findall(text):
        tok = tok.strip(".!?-")
        if not tok or not any(ch.isdigit() for ch in tok):
            continue
        if _IDENT.search(tok):
            _code(ev, tok)
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
                        columns.setdefault(i, []).append(number)
                        if len(rows) <= 25:
                            ev.headline.append(number)
                table.append(line)
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
            .isalnum())
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
    if re.fullmatch(r"[0-9]+[A-Za-z]", fig.text) and fig.text in ev.digits:
        return True   # a code ending in a letter a result holds ("lot 26127B"), no scale
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


def gate(text: str, events: Sequence[Mapping[str, Any]], asked: Sequence[str] = (),
         ev: Optional[Evidence] = None) -> tuple[str, list[str]]:
    """The reply with every unbound figure marked "(unverified)" right after it, and the
    figures marked. A figure the person's own words state (the question, the earlier
    turns) needs no binding. Never removes a word. ``ev``: ``evidence(events)`` when the
    caller already holds it."""
    ev = evidence(events) if ev is None else ev
    own = {f.value for said in asked for f in figures(said)} | {
        abs(f.value) for said in asked for f in figures(said)}
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
        if fig.value in own or abs(fig.value) in own or bound(fig, ev, text[start:end],
                                                               beside):
            ok.add(fig.start)
            held.append(fig)
        elif fig.ratio and _near(ev.values, abs(fig.value), fig.tol):
            held.append(fig)
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
        if (fig.percent or fig.ratio or "." in fig.text) and _compares_line(fig, stated, text):
            continue   # the change, share or quotient of two figures its own line states
        line = text[text.rfind("\n", 0, fig.start) + 1:fig.start]
        if fig.plain and fig.value in items and not _LIST_ITEM.match(line):
            continue   # how many items the reply itself lists (never a cell of a list)
        if fig.plain and re.fullmatch(r"\s*\|\s*", line) \
                and fig.value == _row_number(text, fig.start):
            continue   # a table's own row number ("| 7 | K18 Inc |"), never a figure
        out.append(text[last:fig.end])
        out.append(UNVERIFIED)
        last = fig.end
        marked.append(fig.text)
    out.append(text[last:])
    return "".join(out), marked
