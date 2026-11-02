"""Where one document contradicts itself about what it was given (``plx.read``).

A map or brief often says, near its top, which files it was built from, and states much
later that some of those files were "not provided". An answer about what the document
establishes must keep both statements as an unresolved conflict; a reader who pages
part of the text, or locates passages with ``find``, never sees the two side by side
(RRO-01, RRO-06, RRO-14 on v13: three answers about one map, none named the conflict).
So ``plx.read`` compares them for every read of a document's text, over the whole text
however much of it the read returns, and states each pair it finds as a gap
(``CONFLICT_REASON``) naming both lines.

``stated_conflicts(text)``: each line that says items were not provided, supplied,
attached, included, received, sent or shared (``ABSENT``), the items it names (the list
after its colon, else the clause before the words), and each other line that says the
document was built from, supplied with or holds (``SUPPLIED``, not negated, never a
contract's "provided that" or "as provided in") every distinctive word of one of those
items, in order. An item named only by generic words (``page 2``, ``the attachment``)
names nothing, so it is never matched. Reads nothing, and is bounded: at most
``MAX_SCAN_CHARS`` of the text (to a line end), ``MAX_ABSENT_LINES``, ``MAX_HELD_LINES``,
``MAX_ITEMS`` per line and ``MAX_SCAN_SECONDS``; a scan stopped by a bound says so
(``scan_conflicts``: ``stopped_at_line``), and each text's scan is
kept by the text's sha256 (``CACHE_ENTRIES``), so a document version is scanned once
however many reads of it follow.

``part_line_base(text)``: how many lines of an extraction's text come before the file's
own first line. A file read as one part is joined after a display-only ``[Part: <path>]``
marker (``operating.use_case_parts``): its line N is line N + 2 of the joined text, so a
line a reader names (a card's ``lines 1257-1257``, the file opened in an editor) is
numbered past that marker. A text with any other marker (several parts) keeps whole-text
numbering. Source text is data: it is compared, never interpreted.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import re
from bisect import bisect_right
from threading import Lock
from time import monotonic
from typing import Any

CONFLICT_REASON = "conflicting_sources"
MAX_CONFLICTS = 5
MAX_ABSENT_LINES = 200
MAX_HELD_LINES = 5000
MAX_ITEMS = 30
MAX_SCAN_CHARS = 1_000_000
MAX_SCAN_SECONDS = 1.0
CACHE_ENTRIES = 32
MAX_SNIPPET_CHARS = 240
PART_MARK = "\n[Part: "

# What the document was given, in its own words: an item "not provided" to it, and a
# line saying it was built from, supplied with or provided an item. Process words
# (received, sent, attached to an email) are what a business did, never what the
# document holds, so they are neither.
ABSENT = re.compile(
    r"\b(?:(?:was|were|is|are|has|have)\s+(?:been\s+)?)?(?:not|never)\s+(?:been\s+)?"
    r"(?:provided|supplied|shared\s+with\s+(?:us|me|this))\b", re.I)
# A contract's "provided that", "provided, however", "as provided in", "unless otherwise
# provided" is a condition or a reference, never what the document was given.
SUPPLIED = re.compile(
    r"\b(?:built\s+from|taken\s+from|drawn\s+from|from\s+the\s+files|files?\s+(?:you|"
    r"they|we)\s+(?:sent|shared|supplied|provided)|supplied|"
    r"(?<!\bas\s)(?<!\botherwise\s)provided(?!\s*,?\s*(?:that|however|always)\b))\b",
    re.I)
NEGATED = re.compile(r"\b(?:not|never|no|without)\s+(?:\w+\s+){0,2}$|n't\s+(?:\w+\s+){0,2}$",
                     re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]|[A-Za-z]")
# Words that name no particular thing: an item named only by them is never matched.
GENERIC = frozenset("""
page pages file files document documents attachment attachments copy copies email emails
mail message messages note notes pdf docx xlsx xls csv png jpg jpeg zip txt html line lines
item items sheet sheets photo photos image images scan scans version versions draft drafts
here there this that these those them they their which what with from into onto also only
some each every other others more most such same than then when where while about above
below after before data detail details record records source sources text texts link links
part parts second first third last full the and for are was were been being have
has had not never none yet still both either neither section sections table tables
""".split())


def part_line_base(text: Any) -> int:
    """Lines before a one-part extraction's own first line (module doc); 0 otherwise."""
    if not isinstance(text, str) or not text.startswith(PART_MARK):
        return 0
    end = text.find("]\n", len(PART_MARK))
    if end < 0 or "\n" in text[len(PART_MARK):end] or PART_MARK in text[end:]:
        return 0
    return 2


def _distinct(words: list[str]) -> list[str]:
    return [w for w in words if len(w) >= 4 and w not in GENERIC]


def _items(line: str, match: re.Match) -> list[list[str]]:
    """The distinctive words of each item an absence line names: the list after the
    words' colon, else the clause before them (back to a colon, an opening bracket or a
    sentence end)."""
    after = line[match.end():]
    listed = re.match(r"\s*:\s*(.+)", after)
    if listed:
        span = listed.group(1)
    else:
        before = line[:match.start()]
        cut = max(before.rfind(mark) for mark in (":", "(", "[", ". ", "; "))
        span = before[cut + 1:]
    items = []
    for chunk in re.split(r",|;|\band\b|\bor\b", span):
        # A file name's extension is no word of it; neither is a code with digits.
        chunk = re.sub(r"\.[A-Za-z0-9]{2,5}\b", " ", chunk)
        words = _distinct([w.lower() for w in WORD.findall(chunk)
                           if not re.search(r"\d", w)])
        if words and sum(len(w) for w in words) >= 6:
            items.append(words[:6])
    return items


def _supplies(line: str) -> bool:
    return any(not NEGATED.search(line[:m.start()]) for m in SUPPLIED.finditer(line))


def _snippet(line: str, words: list[str]) -> str:
    """At most ``MAX_SNIPPET_CHARS`` of ``line`` around its first distinctive word."""
    if len(line) <= MAX_SNIPPET_CHARS:
        return line.strip()
    found = re.search(r"\b" + re.escape(words[0]) + r"\b", line, re.I)
    start = max(0, (found.start() if found else 0) - MAX_SNIPPET_CHARS // 2)
    return line[start:start + MAX_SNIPPET_CHARS].strip()


def stated_conflicts(text: Any) -> list[dict[str, Any]]:
    """Each place ``text`` says it was given an item another line says was not provided
    (module doc): ``{"held_line", "absent_line"`` (numbered as ``part_line_base`` says),
    ``"held_offset"``, ``"absent_offset"`` (each line's start in ``text``), ``"held"``,
    ``"absent"`` (the lines' text, at most ``MAX_SNIPPET_CHARS`` each)}, at most
    ``MAX_CONFLICTS``, one per pair of lines, in the order of the absence lines; within
    the scan's bounds (``scan_conflicts``)."""
    return scan_conflicts(text)["conflicts"]


_CACHE: OrderedDict[bytes, dict[str, Any]] = OrderedDict()
_CACHE_LOCK = Lock()


def scan_conflicts(text: Any) -> dict[str, Any]:
    """``{"conflicts": stated_conflicts' list, "stopped_at_line": None when the scan
    covered the whole text, else the first line (numbered as ``part_line_base`` says)
    from which on a bound (module doc) left it unscanned}``; kept per text version (its
    sha256), so a document's many reads scan it once."""
    if not isinstance(text, str) or not text:
        return {"conflicts": [], "stopped_at_line": None}
    key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    found = _scan(text)
    with _CACHE_LOCK:
        _CACHE[key] = found
        while len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return found


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _scan(text: str) -> dict[str, Any]:
    deadline = monotonic() + MAX_SCAN_SECONDS
    base = part_line_base(text)
    scanned = text
    if len(text) > MAX_SCAN_CHARS:
        cut = text.rfind("\n", 0, MAX_SCAN_CHARS)
        scanned = text[:cut] if cut > 0 else ""
    starts = [0] + [i + 1 for i, char in enumerate(scanned) if char == "\n"]
    lines = scanned.split("\n") if scanned else []
    # The first 0-based line no bound let the scan reach (None: none stopped it): an
    # absence line from it on is not compared, a held line from it on is not indexed.
    stop: list[int] = [len(lines)] if scanned is not text else []
    out: list[dict[str, Any]] = []

    def result() -> dict[str, Any]:
        return {"conflicts": out,
                "stopped_at_line": max(1, min(stop) + 1 - base) if stop else None}

    absent = []
    for n, line in enumerate(lines):
        match = ABSENT.search(line)
        if match:
            if len(absent) >= MAX_ABSENT_LINES:
                stop.append(n)
                break
            absent.append((n, match))
    if not absent:
        return result()
    # Held lines indexed by their words: an item is compared only with the lines holding
    # every word of it, never with every held line.
    index: dict[str, list[int]] = {}
    words_of: dict[int, set[str]] = {}
    for h, line in enumerate(lines):
        if h % 256 == 0 and monotonic() > deadline:
            stop.append(h)
            break
        if not _supplies(line):
            continue
        if len(words_of) >= MAX_HELD_LINES:
            stop.append(h)
            break
        words_of[h] = _tokens(line)
        for word in words_of[h]:
            index.setdefault(word, []).append(h)
    pairs: set[tuple[int, int]] = set()
    for n, match in absent:
        items = _items(lines[n], match)
        if len(items) > MAX_ITEMS:
            items = items[:MAX_ITEMS]
            stop.append(n)
        for words in items:
            if monotonic() > deadline:
                stop.append(n)
                return result()
            needed = _tokens(" ".join(words))
            rarest = min(needed, key=lambda word: len(index.get(word, ())))
            phrase = re.compile(r"\b" + r"\W+".join(map(re.escape, words)) + r"\b", re.I)
            for h in index.get(rarest, ()):
                if h == n or (h, n) in pairs or not needed <= words_of[h] \
                        or not phrase.search(lines[h]):
                    continue
                pairs.add((h, n))
                out.append({"held_line": h + 1 - base, "absent_line": n + 1 - base,
                            "held_offset": starts[h], "absent_offset": starts[n],
                            "held": _snippet(lines[h], words),
                            "absent": _snippet(lines[n], words)})
                if len(out) >= MAX_CONFLICTS:
                    return result()
    return result()


def line_of(starts: list[int], offset: int, base: int) -> int:
    """The line (as ``part_line_base`` numbers it) holding ``offset``; at least 1."""
    return max(1, bisect_right(starts, offset) - base)
