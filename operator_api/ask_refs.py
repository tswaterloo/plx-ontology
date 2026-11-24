"""Answers by reference (Ask plan 4.3, A2): the host prints the figures.

The model writes a figure it takes from a ``plx.sql`` result as a reference in double
braces, and the host replaces it with the value from that result, so no count, total,
share or difference is ever typed by the model:

- ``{{sql://<id>#row=3.total}}``: one cell (any column, by its name);
- ``{{count(sql://<id>)}}``: the result's exact row count (the host's profile);
- ``{{sum(sql://<id>.total, CAD)}}``, ``{{min(...)}}``, ``{{max(...)}}``,
  ``{{distinct(sql://<id>.supplier)}}``, ``{{nulls(sql://<id>.purchaser)}}``: from the
  profile of the WHOLE result (a money sum names its currency when it holds several);
- arithmetic over those (``+ - * /``, parentheses, plain numbers) with ``| N`` for N
  decimal places: ``{{sum(sql://a.v, CAD) / sum(sql://b.v, CAD) * 100 | 1}}`` is a share
  of unrounded totals; ``{{days(sql://<id>#row=0.requested_date, today)}}`` the days
  between two dates.

``render`` resolves every reference against the results the session got (its events) and
lists what it could not: a reference to a result, row or column that is not there, and
each figure, date or record number typed outside a reference that no result and none of
the person's own words hold. The caller sends those back to the model once
(``repair_message``); a figure still untraced after that is said in one line under the
answer (the payload's ``untraced``), never tagged inside it. Which text is a figure is read with
one tokenizer (numbers, dates, codes holding digits); what a figure means is the model's
and the checker's business, never decided here."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from . import ask_patch  # noqa: F401  (patches)

TZ = ZoneInfo("America/New_York")
SQL_TOOL = "plx.sql"
DOC_TOOL = "plx.read_document"   # a table:true read is a result too (doc://<id>)
# tools whose results back a typed figure (their text, cells and profiles)
EVIDENCE_TOOLS = ("plx.sql", "plx.search", "plx.open", "plx.read_document", "plx.insight",
                  "plx.my_work", "plx.my_open_work")

_REF = re.compile(r"\{\{(.*?)\}\}", re.S)
_FENCE = re.compile(r"(```.*?(?:```|$))", re.S)
_OPEN_TAIL = re.compile(r"\{\{(?:(?!\}\}).)*$", re.S)
_RID = r"(?:sql|doc)://[0-9a-f]{20}"
_COL = r'(?:"[^"]{1,63}"|[A-Za-z0-9_?][A-Za-z0-9_?]{0,62})'
_TOKEN = re.compile(
    rf"\s*(?:(?P<cell>{_RID}#row=(?P<row>[0-9]{{1,5}})\.(?P<ccol>{_COL}))"
    rf"|(?P<colref>{_RID}\.(?P<col>{_COL}))|(?P<rid>{_RID})"
    r"|(?P<num>[0-9]+(?:\.[0-9]+)?)|(?P<name>[A-Za-z_]+)|(?P<op>[-+*/(),]))")
FUNCS = frozenset({"count", "sum", "min", "max", "distinct", "nulls", "days", "excluded"})
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class RefError(ValueError):
    pass


# ----------------------------------------------------------------------------- results

def _parsed(result: Any) -> Any:
    """A tool result as the session got it (MCP content blocks, a JSON text or a value)."""
    if isinstance(result, list):
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
    if isinstance(result, Mapping) and isinstance(result.get("truncated_text"), str):
        try:
            return json.loads(result["truncated_text"])
        except ValueError:
            return result["truncated_text"]
    return result


def _plain(cell: Any) -> Any:
    """A cell as its value: an untrusted-text envelope's text, else the cell."""
    if isinstance(cell, Mapping) and "untrusted_text" in cell:
        return cell.get("untrusted_text")
    return cell


def _number(value: Any) -> Optional[Decimal]:
    value = _plain(value)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value.strip()):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


@dataclass
class Result:
    columns: list[str]
    rows: dict[int, list[Any]] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    excluded: list[dict[str, Any]] = field(default_factory=list)

    def col(self, name: str) -> int:
        name = name[1:-1] if name.startswith('"') else name
        if name not in self.columns:
            raise RefError(f"no column {name}")
        return self.columns.index(name)

    def numeric(self, name: str) -> bool:
        """Whether the column is a number column (its profile sums it, or says why not);
        unknown (no profile): a number."""
        try:
            got = self.stat(name)
        except RefError:
            return not self.profile
        return "sum" in got

    def stat(self, name: str) -> Mapping[str, Any]:
        cols = self.profile.get("columns")
        if not isinstance(cols, list):
            raise RefError("this result has no profile")
        name = name[1:-1] if name.startswith('"') else name
        got = next((c for c in cols if isinstance(c, Mapping) and c.get("column") == name), None)
        if got is None:
            raise RefError(f"no column {name}")
        return got


def results(events: Sequence[Mapping[str, Any]]) -> dict[str, Result]:
    """Every ``plx.sql`` result the session got, by id: its columns, the rows it showed
    (by their row number in the whole result; a page adds its rows) and its profile."""
    out: dict[str, Result] = {}
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error") \
                or str(event.get("name") or "") not in (SQL_TOOL, DOC_TOOL):
            continue
        got = _parsed(event.get("result"))
        if not isinstance(got, Mapping) or not isinstance(got.get("result"), str) \
                or got.get("error"):
            continue
        if str(event.get("name")) == DOC_TOOL and not got["result"].startswith("doc://"):
            continue
        rid = got["result"]
        kept = out.setdefault(rid, Result(list(got.get("columns") or [])))
        if isinstance(got.get("profile"), Mapping):
            kept.profile = dict(got["profile"])
        if isinstance(got.get("excluded"), list):
            kept.excluded = [x for x in got["excluded"] if isinstance(x, Mapping)]
        for row in got.get("rows") or ():
            if not isinstance(row, Mapping):
                continue
            m = re.search(r"#row=([0-9]+)$", str(row.get("cite") or ""))
            if m and isinstance(row.get("cells"), list):
                kept.rows[int(m.group(1))] = row["cells"]
    return out


def returned(events: Sequence[Mapping[str, Any]]) -> int:
    """How many evidence calls returned a result (a refused or failed one did not)."""
    n = 0
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error") \
                or str(event.get("name") or "") not in EVIDENCE_TOOLS:
            continue
        got = _parsed(event.get("result"))
        if got is None or (isinstance(got, Mapping) and got.get("error")):
            continue
        n += 1
    return n


# ----------------------------------------------------------------------------- values

@dataclass
class Value:
    number: Optional[Decimal] = None
    date: Optional[dt.date] = None
    text: Optional[str] = None
    exact: bool = True    # read as stored (a cell, a count, a sum): printed at its own scale
    money: bool = False   # a column the views type money (profile ``kind``): 2 decimals


def _as_date(raw: Any) -> Optional[dt.date]:
    raw = _plain(raw)
    if not isinstance(raw, str):
        return None
    try:
        if len(raw) == 10:
            return dt.date.fromisoformat(raw)
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(TZ).date() if stamp.tzinfo else stamp.date()


def _cell_value(cell: Any) -> Value:
    number = _number(cell)
    if number is not None:
        return Value(number=number)
    day = _as_date(cell)
    if day is not None:
        return Value(date=day)
    plain = _plain(cell)
    if isinstance(plain, list):
        return Value(text=", ".join(str(_plain(x)) for x in plain))
    if plain is None:
        raise RefError("that cell is empty (NULL)")
    return Value(text=str(plain))


class _Parser:
    def __init__(self, text: str, found: Mapping[str, Result], today: dt.date) -> None:
        self.toks: list[tuple[str, Any]] = []
        at = 0
        while at < len(text):
            m = _TOKEN.match(text, at)
            if m is None or m.end() == at:
                if text[at:].strip():
                    raise RefError(f"cannot read {text[at:at + 20]!r}")
                break
            at = m.end()
            kind = next(k for k in ("cell", "colref", "rid", "num", "name", "op") if m.group(k))
            self.toks.append((kind, m))
        self.i, self.found, self.today = 0, found, today

    def peek(self) -> tuple[Optional[str], Any]:
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, op: Optional[str] = None) -> tuple[str, Any]:
        kind, m = self.peek()
        if kind is None or (op is not None and (kind != "op" or m.group("op") != op)):
            raise RefError(f"expected {op or 'more'}")
        self.i += 1
        return kind, m

    @staticmethod
    def _money(res: Result, column: str) -> bool:
        """Whether the host's profile says the column is money (its source column's declared
        kind, ``result_profile.roles``): never read from a name or a value."""
        try:
            return res.stat(column).get("kind") == "money"
        except RefError:
            return False

    def result(self, rid: str) -> Result:
        got = self.found.get(rid)
        if got is None:
            raise RefError(f"{rid} is not a result you got in this answer")
        return got

    def expr(self) -> Value:
        left = self.term()
        while True:
            kind, m = self.peek()
            if kind == "op" and m.group("op") in "+-":
                self.i += 1
                right = self.term()
                left = _arith(left, right, m.group("op"))
            else:
                return left

    def term(self) -> Value:
        left = self.unary()
        while True:
            kind, m = self.peek()
            if kind == "op" and m.group("op") in "*/":
                self.i += 1
                right = self.unary()
                left = _arith(left, right, m.group("op"))
            else:
                return left

    def unary(self) -> Value:
        kind, m = self.peek()
        if kind == "op" and m.group("op") == "-":
            self.i += 1
            v = self.unary()
            return _arith(Value(number=Decimal(0)), v, "-")
        return self.atom()

    def atom(self) -> Value:
        kind, m = self.take()
        if kind == "num":
            return Value(number=Decimal(m.group("num")), exact=False)
        if kind == "op" and m.group("op") == "(":
            v = self.expr()
            self.take(")")
            return Value(number=v.number, date=v.date, text=v.text, exact=False)
        if kind == "cell":
            res = self.result(m.group("cell").split("#", 1)[0])
            row = int(m.group("row"))
            if row not in res.rows:
                raise RefError(f"row {row} of {m.group('cell').split('#', 1)[0]} was not shown "
                               "to you (page it first)")
            got = _cell_value(res.rows[row][res.col(m.group("ccol"))])
            if got.number is not None and self._money(res, m.group("ccol")):
                got = replace(got, money=True)
            if got.number is not None and not res.numeric(m.group("ccol")):
                # a number held in a text column (a PO or item number): printed as stored
                return Value(text=_plain(res.rows[row][res.col(m.group("ccol"))]))
            return got
        if kind == "name" and m.group("name") == "today":
            return Value(date=self.today)
        if kind == "name" and m.group("name") in FUNCS:
            return self.call(m.group("name"))
        raise RefError(f"cannot read {m.group(0).strip()!r}")

    def call(self, name: str) -> Value:
        self.take("(")
        if name == "days":
            a = self.expr()
            self.take(",")
            b = self.expr()
            self.take(")")
            if a.date is None or b.date is None:
                raise RefError("days() takes two dates (date cells or today)")
            return Value(number=Decimal((b.date - a.date).days))
        kind, m = self.take()
        if name == "excluded":
            if kind != "rid":
                raise RefError("excluded() takes a measure's result and a parameter: "
                               "excluded(sql://<id>, <parameter>)")
            self.take(",")
            pkind, pm = self.take()
            self.take(")")
            got = next((x for x in self.result(m.group("rid")).excluded
                        if pm is not None and x.get("parameter") == pm.group(0).strip()), None)
            # the number of records the parameter leaves out (S2: excluded n)
            if got is None or not isinstance(got.get("n"), int) or isinstance(got.get("n"), bool):
                raise RefError("that result states no count for that exclusion")
            return Value(number=Decimal(got["n"]))
        if name == "count":
            if kind != "rid":
                raise RefError("count() takes a result: count(sql://<id>)")
            self.take(")")
            rows = self.result(m.group("rid")).profile.get("rows")
            if not isinstance(rows, int) or isinstance(rows, bool):
                raise RefError("this result's exact row count is not known: count with a query")
            return Value(number=Decimal(rows))
        if kind != "colref":
            raise RefError(f"{name}() takes a column: {name}(sql://<id>.<column>)")
        res = self.result(m.group("colref")[:len("sql://") + 20])
        if res.profile.get("exact") is False:
            # the profile covers only the rows the host held (its whole-result statement
            # failed): never printed as a figure of the whole result
            raise RefError("this result's column figures cover only part of it ("
                           + str(_plain(res.profile.get("note")) or "the whole could not be "
                                 "counted") + "): read the figure with a query (COUNT, SUM, "
                           "GROUP BY)")
        stat = res.stat(m.group("col"))
        currency = None
        if self.peek()[0] == "op" and self.peek()[1].group("op") == ",":
            self.i += 1
            ckind, cm = self.take()
            if ckind != "name":
                raise RefError("the currency is a code: sum(sql://<id>.total, CAD)")
            currency = cm.group("name")
        self.take(")")
        if name == "nulls" and res.profile.get("nulls_include_withheld"):
            raise RefError("this result's NULLs include cells withheld from this reader: "
                           "say the answer is partial instead of a count of empty cells")
        if name in ("distinct", "nulls"):
            got = stat.get(name)
            if not isinstance(got, int) or isinstance(got, bool):
                raise RefError(f"no {name} count for that column")
            return Value(number=Decimal(got))
        raw = stat.get(name)
        if name == "sum":
            if raw is None:
                raise RefError(str(_plain(stat.get("note")) or "that column is not summed"))
            if isinstance(raw, list):
                by = {str(_plain(x.get("currency"))): x.get("sum") for x in raw
                      if isinstance(x, Mapping)}
                if currency is None:
                    if len(by) != 1:
                        raise RefError("that sum is per currency (" + ", ".join(sorted(by))
                                       + "): name one, sum(sql://<id>.<column>, CAD)")
                    raw = next(iter(by.values()))
                elif currency not in by:
                    raise RefError(f"no {currency} sum in that column")
                else:
                    raw = by[currency]
            elif currency is not None:
                raise RefError("that sum is not per currency")
        if raw is None:
            raise RefError(f"no {name} for that column")
        cell = _cell_value(raw)
        return replace(cell, money=True) if cell.number is not None \
            and stat.get("kind") == "money" else cell


def _arith(a: Value, b: Value, op: str) -> Value:
    if a.number is None or b.number is None:
        raise RefError("arithmetic takes numbers")
    if op == "+":
        n = a.number + b.number
    elif op == "-":
        n = a.number - b.number
    elif op == "*":
        n = a.number * b.number
    else:
        if b.number == 0:
            raise RefError("division by zero")
        n = a.number / b.number
    return Value(number=n, exact=False)


def _grouped(number: Decimal) -> str:
    sign = "-" if number < 0 else ""
    text = format(abs(number), "f")
    whole, _, frac = text.partition(".")
    whole = f"{int(whole):,}"
    return sign + whole + (("." + frac) if frac else "")


def show(value: Value, places: Optional[int]) -> str:
    """A value as the answer prints it: a number with thousands separators at its own
    scale (a cell, a count, a sum) or rounded to ``places`` (computed: 2 by default when
    not whole); a date as the app writes it; text as stored."""
    if value.number is not None:
        n = value.number
        if places is not None:
            n = n.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        elif value.money:
            n = n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)   # money: 2 decimals
        elif value.exact and n != n.to_integral_value():
            # another stored number: no trailing zeros, no more than 4 decimals
            n = n.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            n = n.normalize() if n != n.to_integral_value() else n.to_integral_value()
        elif not value.exact and n != n.to_integral_value():
            n = n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif not value.exact:
            n = n.to_integral_value()
        return _grouped(n)
    if value.date is not None:
        return f"{value.date.day} {MONTHS[value.date.month - 1]} {value.date.year}"
    return str(value.text)


def evaluate(body: str, found: Mapping[str, Result], today: dt.date) -> str:
    """One reference's text (between the braces) as the host prints it."""
    expr, places = body, None
    m = re.search(r"\|\s*([0-9])\s*$", body)
    if m:
        expr, places = body[:m.start()], int(m.group(1))
    p = _Parser(expr, found, today)
    value = p.expr()
    if p.i != len(p.toks):
        raise RefError(f"cannot read {p.toks[p.i][1].group(0).strip()!r}")
    if value.text is not None and places is not None:
        raise RefError("decimal places go with a number")
    try:
        return show(value, places)
    except InvalidOperation:
        raise RefError("that figure is too large to print at those decimals") from None


# ----------------------------------------------------------------------------- typed figures

# the calendar's month names, read as dates (a tokenizer of dates, never a meaning)
_DATE_DM = re.compile(  # guard-ok: month names of a date
    r"(?<![\w-])(3[01]|[12][0-9]|0?[1-9])\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
    r"Sep|Sept|Oct|Nov|Dec)[a-z]*\.?(?:,?\s+((?:19|20)[0-9]{2}))?(?![\w-])")
_DATE_MD = re.compile(  # guard-ok: month names of a date
    r"(?<![\w-])(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
    r"\s+(3[01]|[12][0-9]|0?[1-9])(?:st|nd|rd|th)?(?:,?\s+((?:19|20)[0-9]{2}))?"
    r"(?![\w:%])")
_DATE_ISO = re.compile(r"(?<![\w/.-])((?:19|20)[0-9]{2})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
                       r"(?:[T ][0-9:.+Z-]{4,})?(?![\w/-])")
_NUMBER = re.compile(r"(?<![\w./:#-])(?<![0-9],)-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
                     r"(?![\w/:]|\.[0-9]|,[0-9]{3})")
# a record number: letters and at least three digits in one token (PO100109, R-0098,
# OR1000153951); shorter mixes are labels (Q3, H1, B12), never checked as records
_CODE = re.compile(r"(?<![\w/.:#@-])(?=(?:[\w./#-]*?[0-9]){3})(?=[\w./#-]*[A-Za-z])[A-Za-z0-9]"
                   r"[\w./#-]*[A-Za-z0-9](?![\w/@-])")
_UNIT_NUMBER = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]{1,3})$")   # 20L, 5ml, 25kg
_SKIP_BLOCKS = re.compile(r"```.*?(?:```|$)|`[^`\n]*`|\]\([^)]*\)|https?://\S+", re.S)
_LIST_MARK = re.compile(r"(?m)^(\s*)[0-9]{1,3}[.)](?=\s)")
_MONTH_NO = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}


def _month(word: str) -> int:
    return _MONTH_NO[word[:3].lower()]  # guard-ok: a month name's first three letters, not a cut


def _dates_in(text: str) -> set[tuple[int, int, Optional[int]]]:
    out: set[tuple[int, int, Optional[int]]] = set()
    for m in _DATE_DM.finditer(text):
        out.add((_month(m.group(2)), int(m.group(1)),
                 int(m.group(3)) if m.group(3) else None))
    for m in _DATE_MD.finditer(text):
        out.add((_month(m.group(1)), int(m.group(2)),
                 int(m.group(3)) if m.group(3) else None))
    for m in _DATE_ISO.finditer(text):
        out.add((int(m.group(2)), int(m.group(3)), int(m.group(1))))
        stamp = _as_date(m.group(0))   # a timestamp's day in the business time zone too
        if stamp is not None:
            out.add((stamp.month, stamp.day, stamp.year))
    return out


def _numbers_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in _NUMBER.finditer(text):
        try:
            d = Decimal(m.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        out.update({d, abs(d)})
    return out


@dataclass
class Backing:
    """What a typed figure may rest on: every number, date and code the session's results
    and the person's own words hold."""
    numbers: set[Decimal] = field(default_factory=set)
    dates: set[tuple[int, int, Optional[int]]] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)

    def read(self, text: str) -> None:
        self.numbers |= _numbers_in(text)
        for (mo, d, y) in _dates_in(text):
            self.dates.add((mo, d, y))
            if y is not None:
                self.numbers.add(Decimal(y))
        self.codes |= {c.lower() for c in _CODE.findall(text)}
        # a code's own digits ("PO100109" quoted as "100109") and a number's unit
        for code in _CODE.findall(text):
            u = _UNIT_NUMBER.match(code)
            if u:
                self.numbers.add(Decimal(u.group(1)))

    def holds_date(self, mo: int, d: int, y: Optional[int]) -> bool:
        return (mo, d, y) in self.dates or (y is None and any(
            (a, b) == (mo, d) for a, b, _ in self.dates)) or (mo, d, None) in self.dates


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if k not in ("cite", "result", "ref", "handle", "citation", "origin"):
                yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        yield format(Decimal(repr(value)) if isinstance(value, float) else Decimal(value), "f")


def backing(events: Sequence[Mapping[str, Any]], asked: Sequence[str] = (),
            files: Sequence[str] = (), today: Optional[dt.date] = None) -> Backing:
    back = Backing()
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error") \
                or str(event.get("name") or "") not in EVIDENCE_TOOLS:
            continue
        got = _parsed(event.get("result"))
        if isinstance(got, Mapping) and got.get("error"):
            continue
        for text in _strings(got):
            back.read(text)
            number = _number(text)
            if number is not None:
                back.numbers.update({number, abs(number)})
            day = _as_date(text)
            if day is not None:
                back.dates.add((day.month, day.day, day.year))
    for said in [*asked, *files]:
        back.read(str(said or ""))
    today = today or dt.datetime.now(TZ).date()
    for day in (today, today - dt.timedelta(days=1)):
        back.dates.add((day.month, day.day, day.year))
        back.numbers.add(Decimal(day.year))
    return back


def typed(text: str, back: Backing) -> list[str]:
    """The figures, dates and codes typed in ``text`` (references already printed or cut
    out) that ``back`` does not hold, each once, in order."""
    plain = _SKIP_BLOCKS.sub(" ", text)
    plain = _LIST_MARK.sub(r"\1", plain)
    plain = re.sub(r"(?m)^\s*#{1,6}\s", "", plain)
    out: list[str] = []

    def miss(what: str) -> None:
        what = what.strip().rstrip(".,;:")
        if what not in out:
            out.append(what)
    spans: list[tuple[int, int]] = []
    for rx, order in ((_DATE_DM, "dm"), (_DATE_MD, "md"), (_DATE_ISO, "iso")):
        for m in rx.finditer(plain):
            if order == "iso":
                mo, d, y = int(m.group(2)), int(m.group(3)), int(m.group(1))
            elif order == "dm":
                mo = _month(m.group(2))
                d, y = int(m.group(1)), int(m.group(3)) if m.group(3) else None
            else:
                mo = _month(m.group(1))
                d, y = int(m.group(2)), int(m.group(3)) if m.group(3) else None
            spans.append(m.span())
            if not back.holds_date(mo, d, y):
                miss(m.group(0).strip())

    def free(start: int, end: int) -> bool:
        return not any(a <= start < b or a < end <= b for a, b in spans)
    for m in _CODE.finditer(plain):
        if not free(*m.span()):
            continue
        u = _UNIT_NUMBER.match(m.group(0))
        if u:   # a number with its unit: the number is the figure
            if Decimal(u.group(1)) not in back.numbers:
                miss(m.group(0))
        elif m.group(0).lower() not in back.codes:
            miss(m.group(0))
        spans.append(m.span())
    for m in _NUMBER.finditer(plain):
        if not free(*m.span()):
            continue
        try:
            d = Decimal(m.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        if d not in back.numbers and abs(d) not in back.numbers:
            miss(m.group(0))
    return out


# ----------------------------------------------------------------------------- render

@dataclass
class Rendered:
    text: str
    bad: list[str]       # references that did not resolve, with why
    untraced: list[str]  # typed figures no result or the person's words hold
    working: list[str] = field(default_factory=list)   # each printed figure and where it came from


def render(text: str, events: Sequence[Mapping[str, Any]], asked: Sequence[str] = (),
           files: Sequence[str] = (), today: Optional[dt.date] = None, *,
           partial: bool = False, back: Optional[Backing] = None) -> Rendered:
    """``text`` with every reference printed by the host. ``partial`` (a stream still
    being written): an unclosed reference at the end is held back, never shown half."""
    today = today or dt.datetime.now(TZ).date()
    sums: list[str] = []
    printed, bad = print_refs(text, events, today, partial=partial, working=sums)
    # typed figures are read on the text outside the references (what the host printed
    # is the results' own)
    outside = _REF.sub(" ", text)
    back = back or backing(events, asked, files, today)
    return Rendered(printed, bad, typed(outside, back), sums)


def source_of(body: str, found: Mapping[str, "Result"], events: Sequence[Mapping[str, Any]]
              ) -> str:
    """What the results a reference reads are: a document table by its document and sheet
    (``plx.read_document`` table:true), a statement result by its first statement words."""
    out = []
    for rid in dict.fromkeys(re.findall(_RID, body)):
        label = rid
        for event in events:
            if event.get("type") != "tool_call" or event.get("is_error"):
                continue
            got = _parsed(event.get("result"))
            if not isinstance(got, Mapping) or got.get("result") != rid:
                continue
            doc = got.get("document")
            if isinstance(doc, Mapping):
                args = event.get("arguments") if isinstance(event.get("arguments"), Mapping) \
                    else {}
                again = {k: v for k, v in args.items() if k in (
                    "citation", "member", "sheet", "header_row", "where")}
                label = (f"document {_plain(got.get('name')) or doc.get('citation')}, sheet "
                         f"{doc.get('sheet')} ({got.get('row_count')} rows; read again with "
                         f"plx.read_document table:true {json.dumps(again, default=str)})")
            else:
                args = event.get("arguments") if isinstance(event.get("arguments"), Mapping) \
                    else {}
                sql = " ".join(str(args.get("sql") or "").split())
                label = f"the statement {sql}" if sql else f"result {rid}"
            break
        out.append(label)
    return "; ".join(out) or "the numbers written in the expression"


def print_refs(text: str, events: Sequence[Mapping[str, Any]],
               today: Optional[dt.date] = None, *, partial: bool = False,
               results_only: bool = False,
               found: Optional[Mapping[str, "Result"]] = None,
               working: Optional[list[str]] = None) -> tuple[str, list[str]]:
    """(``text`` with every reference printed from the session's results, the references
    that did not resolve with why): the one renderer of every session given the Ask's
    prompt (the Ask, autopilot drafts, jobs and the assistant, made documents).
    ``results_only``: only references to a result (``sql://``) are printed; any other
    double-brace text is left as written (a template's ``{{po_number}}`` field).
    ``working`` (a list the caller keeps): each printed reference as "expression = value,
    from <what the result is>", so a figure the host computed can be checked by hand
    (``source_of``), once per distinct expression."""
    today = today or dt.datetime.now(TZ).date()
    found = results(events) if found is None else found   # ``found``: read once by the caller
    if partial:
        text = _OPEN_TAIL.sub("", text)
    bad: list[str] = []
    pieces: list[str] = []
    for n, part in enumerate(_FENCE.split(text)):
        if n % 2:   # a fenced block (a chart's spec): left as written
            pieces.append(part)
            continue
        last = 0
        for m in _REF.finditer(part):
            if results_only and not re.search(r"(?:sql|doc)://", m.group(1)):
                continue
            pieces.append(part[last:m.start()])
            last = m.end()
            try:
                shown = evaluate(m.group(1), found, today)
                pieces.append(shown)
                if working is not None:
                    line = f"{' '.join(m.group(1).split())} = {shown}, from " \
                           + source_of(m.group(1), found, events)
                    if line not in working:
                        working.append(line)
            except RefError as exc:
                bad.append(f"{{{{{m.group(1).strip()}}}}}: {exc}")
                pieces.append("[?]")
        pieces.append(part[last:])
    return "".join(pieces), bad


def repair_message(rendered: Rendered) -> Optional[str]:
    """The one message that sends an answer back for repair (None: nothing to repair)."""
    if not rendered.bad and not rendered.untraced:
        return None
    lines = ["Your answer cannot be shown as written:"]
    if rendered.bad:
        lines.append("These references do not resolve:")
        lines += [f"- {b}" for b in rendered.bad]
    if rendered.untraced:
        lines.append("These figures, dates or record numbers are typed, and no result you "
                     "got (nor the person's own words) holds them exactly: "
                     + ", ".join(rendered.untraced) + ".")
    lines.append("Write the whole answer again. Give every count, total, share or difference "
                 "as a reference to the result it comes from ({{count(sql://<id>)}}, "
                 "{{sum(sql://<id>.<column>, CAD)}}, {{a / b * 100 | 1}}, "
                 "{{sql://<id>#row=N.<column>}}); run plx.sql first if no result holds it. "
                 "Quote a figure from a message or document exactly as it is written there. "
                 "Leave out what you cannot back.")
    return "\n".join(lines)


def patches(text: str, rendered: Rendered, events: Sequence[Mapping[str, Any]]
            ) -> list["ask_patch.Patch"]:
    """The sentences of ``text`` (the answer as the session wrote it) that hold a reference
    that does not resolve or a typed figure no result holds, one patch per sentence with
    every reason for it (``ask_patch``: the session replaces those sentences, not the
    whole answer). A problem whose sentence cannot be found is not lost: ``None`` is never
    returned for it, the caller falls back to ``repair_message`` (``unplaced``)."""
    from . import ask_patch
    today = dt.datetime.now(TZ).date()
    found = results(events)
    reasons: dict[tuple[int, int], list[str]] = {}
    for n, part in enumerate(_FENCE.split(text)):
        if n % 2:
            continue
        base = text.find(part) if part else 0
        for m in _REF.finditer(part):
            try:
                evaluate(m.group(1), found, today)
            except RefError as exc:
                at = ask_patch.sentence_with(text, m.group(0), max(base, 0))
                if at:
                    reasons.setdefault(at, []).append(
                        f"the reference {{{{{m.group(1).strip()}}}}} does not resolve: {exc}")
    outside = _REF.sub(" ", text)
    for fig in rendered.untraced:
        at = ask_patch.sentence_with(text, fig)
        if at is None:
            at = ask_patch.sentence_with(outside, fig)
        if at:
            reasons.setdefault(at, []).append(
                f"{fig} is typed and no result you got holds it exactly")
    return [ask_patch.Patch(a, b, text[a:b], "; ".join(r)) for (a, b), r in sorted(reasons.items())]


def unplaced(text: str, rendered: Rendered, patched: Sequence["ask_patch.Patch"]) -> int:
    """How many of the rendering's problems no patch carries (a sentence not found)."""
    return max(0, len(rendered.bad) + len(rendered.untraced)
               - sum(p.why.count("; ") + 1 for p in patched))


def patch_message(patched: Sequence["ask_patch.Patch"]) -> str:
    from . import ask_patch
    return ask_patch.message(
        patched, "These sentences of your answer cannot be shown as written:",
        "Give every count, total, share or difference as a reference to the result it comes "
        "from ({{count(sql://<id>)}}, {{sum(sql://<id>.<column>, CAD)}}, {{a / b * 100 | 1}}, "
        "{{sql://<id>#row=N.<column>}}); run plx.sql first if no result holds it. Quote a "
        "figure from a message or document exactly as it is written there. Say 'drop' for a "
        "sentence you cannot back.")


UNTRACED = "Not traced to a record:"



# ----------------------------------------------------------------------------- tools

REF_REFUSED = "unresolved_reference"


def _printed_args(value: Any, found: Mapping[str, "Result"], bad: list[str]) -> Any:
    if isinstance(value, str):
        if "{{" not in value:
            return value
        text, wrong = print_refs(value, (), results_only=True, found=found)
        bad += wrong
        return text
    if isinstance(value, list):
        return [_printed_args(v, found, bad) for v in value]
    if isinstance(value, Mapping):
        return {k: _printed_args(v, found, bad) for k, v in value.items()}
    return value


def rendering(tool: Any) -> Any:
    """``tool`` (a session's ``LocalTool``: a write, a document, a proposal) with the
    references in its arguments printed from what the session's reads returned before the
    call is made (A2a): the file or change holds the host's figures, never ``{{...}}``. A
    reference that does not resolve refuses the call with each one named and why (no file
    or change with a hole); the tool itself is never called then."""
    from operating.agent_session import LocalTool

    def call(arguments: dict[str, Any], seen: Sequence[Mapping[str, Any]] = ()) -> Any:
        bad: list[str] = []
        printed = _printed_args(arguments, results(seen), bad)   # the results read once
        if bad:
            return {"ok": False, "error": REF_REFUSED, "references": bad,
                    "message": "these references do not resolve: write each from a result "
                               "of this session (run plx.sql first) and call again"}
        return tool.call(printed)
    return LocalTool(spec=tool.spec, call=call, sees_results=True)
