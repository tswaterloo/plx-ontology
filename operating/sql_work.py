"""Show the work of one read statement, from the statement itself: the business views it
reads and the conditions it filters on, each a span of its text a reader can change or
take out (Genie plan v2 item 1: an answer's or a tile's applied filters, editable).

A filter is one AND-part of a WHERE or HAVING clause, at the depth that clause is written
(a BETWEEN's AND and a CASE's are inside one part); a clause inside another filter's text
(a subquery's) is part of that filter, never a second chip over the same text. A
statement this scanner does not read whole (a comment, a dollar or escape string, an
unbalanced parenthesis, an unterminated string or identifier) has no filters: no chip is
offered on text that is not understood (fail closed). The statement an edit makes is
checked again as ``plx.sql`` checks any statement and is read through the reader's own
door, so a change can only ever narrow or widen within what the reader may read.

Edits: ``{"index": i, "text": "..."}`` puts ``(text)`` in place of filter ``i``;
``{"index": i, "text": null}`` takes it out (it becomes ``TRUE``, so the clause stays
well formed). An index the statement does not have, the same filter twice, or a text that
is not one balanced condition (a ``;``, an unbalanced parenthesis, a comment or a
clause keyword at its top level) is refused (``Unworkable``)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional, Sequence

MAX_FILTERS = 12        # chips shown per statement; the rest are counted (``more``)
MAX_EDIT = 1000         # characters of one changed condition

# the words that end a WHERE/HAVING clause at its own depth
_ENDS = frozenset("""group order limit offset having window union intersect except fetch
for returning qualify""".split())
# words a changed condition may not hold at its top level (it is one condition)
_CLAUSE = _ENDS | frozenset("select from where with join on using".split())


class Unworkable(ValueError):
    """An edit that does not make one statement with one condition changed."""


@dataclass(frozen=True)
class Token:
    kind: str      # word, string, qident, number, punct
    text: str
    start: int
    end: int


def tokens(sql: str) -> Optional[list[Token]]:
    """The statement's tokens with their places; None for text not read whole."""
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i) or sql.startswith("/*", i) or ch in "$\\":
            return None
        elif ch == "'":
            if out and out[-1].kind == "word" and out[-1].end == i \
                    and out[-1].text.lower() in ("e", "u&", "u", "b", "x", "n"):
                return None
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j < 0:
                    return None
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            out.append(Token("string", sql[i:j + 1], i, j + 1))
            i = j + 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            while j >= 0 and sql.startswith('""', j):
                j = sql.find('"', j + 2)
            if j < 0:
                return None
            out.append(Token("qident", sql[i:j + 1], i, j + 1))
            i = j + 1
        elif ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_"):
                j += 1
            out.append(Token("word", sql[i:j], i, j))
            i = j
        elif ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            out.append(Token("number", sql[i:j], i, j))
            i = j
        else:
            out.append(Token("punct", ch, i, i + 1))
            i += 1
    depth = 0
    for t in out:
        depth += (t.text == "(") - (t.text == ")") if t.kind == "punct" else 0
        if depth < 0:
            return None
    return out if depth == 0 else None


def _low(t: Token) -> str:
    return t.text.lower() if t.kind == "word" else ""


def _parts(toks: list[Token], depth_at: list[int], lo: int, d: int) -> list[list[Token]]:
    """The AND-parts of the clause starting at token ``lo`` written at depth ``d``."""
    parts: list[list[Token]] = [[]]
    between = case = 0
    for j in range(lo, len(toks)):
        u = toks[j]
        if depth_at[j] < d or (u.kind == "punct" and u.text == ";"):
            break
        if depth_at[j] == d:
            w = _low(u)
            if w in _ENDS and not case:
                break
            if w == "case":
                case += 1
            elif w == "end" and case:
                case -= 1
            elif w == "between":
                between += 1
            elif w == "and" and not case:
                if between:
                    between -= 1
                else:
                    parts.append([])
                    continue
        parts[-1].append(u)
    return [p for p in parts if p]


def _at(toks: list[Token], t: Token) -> int:
    """``t``'s place in ``toks`` (tokens are in text order: by its start)."""
    import bisect
    return bisect.bisect_left([u.start for u in toks], t.start)


def _spans(toks: list[Token], depth_at: list[int], part: list[Token]) -> list[tuple[int, int]]:
    """One part's span; a part that is one parenthesized group of several AND-parts
    (``(a AND b)``, a measure's own condition) is those parts, each its own."""
    first, last = _at(toks, part[0]), _at(toks, part[-1])
    if part[0].text == "(" and part[-1].text == ")" and part[0].kind == "punct" \
            and last > first + 1 and all(depth_at[j] > depth_at[first]
                                         for j in range(first + 1, last)):
        inner = _parts(toks, depth_at, first + 1, depth_at[first] + 1)
        # ``(a AND b)`` is its parts; so is ``((a AND b))`` (a measure's own condition
        # wrapped once more): a group holding only another group is looked into
        if len(inner) > 1 or (len(inner) == 1 and inner[0][0].text == "("
                              and inner[0][-1].text == ")" and inner[0][0].kind == "punct"):
            return [s for q in inner for s in _spans(toks, depth_at, q)]
    return [(part[0].start, part[-1].end)]


def filters(sql: str) -> tuple[list[tuple[int, int]], int]:
    """The statement's filters as (start, end) spans of its text, in order, at most
    ``MAX_FILTERS``; and how many more there are. None read: ([], 0)."""
    toks = tokens(sql)
    if not toks:
        return [], 0
    depth_at: list[int] = []
    depth = 0
    for t in toks:
        if t.kind == "punct" and t.text == ")":
            depth -= 1
        depth_at.append(depth)
        if t.kind == "punct" and t.text == "(":
            depth += 1
    spans: list[tuple[int, int]] = []
    for k, t in enumerate(toks):
        if _low(t) not in ("where", "having"):
            continue
        # an aggregate's FILTER (WHERE ...) is part of what the figure counts, not a filter
        if _low(t) == "where" and k >= 2 and toks[k - 1].text == "(" \
                and _low(toks[k - 2]) == "filter":
            continue
        for part in _parts(toks, depth_at, k + 1, depth_at[k]):
            spans += _spans(toks, depth_at, part)
    # a clause inside another filter's text (a subquery's) belongs to that filter
    outer = sorted({s for s in spans if not any(o != s and o[0] <= s[0] and s[1] <= o[1]
                                                for o in spans)})
    return outer[:MAX_FILTERS], max(0, len(outer) - MAX_FILTERS)


def views(sql: str, known: Iterable[str]) -> list[str]:
    """The business views among ``known`` the statement names as identifiers (never in a
    string), in the order first named."""
    toks = tokens(sql) or []
    names = {v.lower(): v for v in known}
    out: list[str] = []
    for t in toks:
        name = t.text.lower() if t.kind == "word" else (
            t.text[1:-1] if t.kind == "qident" else "")
        if name in names and names[name] not in out:
            out.append(names[name])
    return out


def work(sql: Any, known: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    """What ``sql`` reads and filters on, for ``models.QueryWork``; None without a
    statement. Read once per statement (a dashboard's tiles repeat theirs on every open)."""
    if not isinstance(sql, str) or not sql.strip():
        return None
    seen, spans, more = _work(sql, tuple(known))
    return {"views": list(seen),
            "filters": [{"index": i, "text": sql[a:b]} for i, (a, b) in enumerate(spans)],
            "more_filters": more}


@lru_cache(maxsize=512)
def _work(sql: str, known: tuple[str, ...]) -> tuple[tuple[str, ...], tuple, int]:
    spans, more = filters(sql)
    return tuple(views(sql, known)), tuple(spans), more


def _one_condition(text: str) -> str:
    toks = tokens(text)
    if not toks or len(text) > MAX_EDIT:
        raise Unworkable("the condition is empty, too long, or not one whole condition")
    depth = 0
    for t in toks:
        if t.kind == "punct" and t.text in "()":
            depth += 1 if t.text == "(" else -1
        elif t.kind == "punct" and t.text == ";":
            raise Unworkable("a condition holds no ';'")
        elif depth == 0 and _low(t) in _CLAUSE:
            raise Unworkable(f"a condition holds no {t.text.upper()} of its own")
    return text.strip()


def edited(sql: str, edits: Sequence[Mapping[str, Any]]) -> str:
    """``sql`` with each edit's filter changed or taken out (module docstring)."""
    spans, _more = filters(sql)
    if not edits:
        raise Unworkable("no filter was changed")
    seen: set[int] = set()
    changes: list[tuple[int, int, str]] = []
    for e in edits:
        i = e.get("index")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(spans) \
                or i in seen:
            raise Unworkable("the statement has no such filter (or it was named twice)")
        seen.add(i)
        text = e.get("text")
        if text is not None and not isinstance(text, str):
            raise Unworkable("a filter's text is words or nothing")
        a, b = spans[i]
        changes.append((a, b, "TRUE" if text is None else f"({_one_condition(text)})"))
    out = sql
    for a, b, new in sorted(changes, reverse=True):
        out = out[:a] + new + out[b:]
    return out
