"""One sentence of an answer replaced, never the whole answer written again (Fable spec
16.2 item 5 and 16.4 F-R1: the whole-answer repair turn made the model rewrite its answer in
19 of 42 answers). A host finds the sentences that need a second look (a reference that does
not resolve, a figure no result holds, a sentence the covering-read pass listed), sends them
back numbered with why, and swaps in only what the session returns for each number:

* ``N. <new sentence>`` replaces sentence N;
* ``N. stands`` keeps it as written (a flag that proved wrong costs a read and changes
  nothing);
* ``N. drop`` takes it out;
* a number the reply does not mention keeps its sentence, and the turn says so
  (``Applied``: ``kept`` lists them).

A sentence is a line or a run ending at . ! or ? followed by a space: the split is of the
text's layout only and reads no meaning from it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*{\"'(\[])")
STANDS = "stands"
DROP = "drop"


@dataclass
class Patch:
    start: int
    end: int
    sentence: str
    why: str
    ask: str = ""


@dataclass
class Applied:
    text: str
    replaced: int = 0
    dropped: int = 0
    stood: int = 0
    kept: list[int] = field(default_factory=list)   # numbers the reply did not answer
    changes: list[dict] = field(default_factory=list)   # {n, was, now}: what each swap replaced


def spans(text: str) -> list[tuple[int, int]]:
    """The sentences of ``text`` as (start, end), in order; fenced blocks and table rows
    are never split (a row is one unit), blank lines belong to no sentence."""
    out: list[tuple[int, int]] = []
    fenced = False
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        start = pos
        pos += len(line)
        if body.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not body.strip():
            continue
        if body.lstrip().startswith("|"):
            out.append((start, start + len(body)))
            continue
        last = 0
        for m in _END.finditer(body):
            if body[last:m.start()].strip():
                out.append((start + last, start + m.start()))
            last = m.end()
        if body[last:].strip():
            out.append((start + last, start + len(body)))
    return out


def sentence_with(text: str, needle: str, after: int = 0) -> Optional[tuple[int, int]]:
    """The span of the first sentence holding ``needle`` (at or after ``after``)."""
    at = text.find(needle, after)
    if at < 0:
        return None
    return next(((a, b) for a, b in spans(text) if a <= at < b), None)


def find(text: str, quoted: str) -> Optional[tuple[int, int]]:
    """The span of the sentence a flag quotes: the exact sentence, else the one holding the
    most of its words' opening (a model quotes with its own spacing)."""
    quoted = " ".join(str(quoted or "").split())
    if not quoted:
        return None
    hit = sentence_with(text, quoted)
    if hit:
        return hit
    flat = [(a, b, " ".join(text[a:b].split())) for a, b in spans(text)]
    for a, b, s in flat:
        if quoted in s or s in quoted and len(s) > 12:
            return a, b
    return None


def message(patches: Sequence[Patch], intro: str, how: str) -> str:
    lines = [intro]
    for n, p in enumerate(patches, 1):
        lines.append(f'{n}. "{p.sentence.strip()}"\n   Why: {p.why}'
                     + (f"\n   Settle it with: {p.ask}" if p.ask else ""))
    lines.append(how)
    lines.append('Reply with one line per number and nothing else: "N. <the replacement for '
                 'that one sentence>", or "N. stands" if the read shows it holds as written, '
                 'or "N. drop" to leave it out. The rest of your answer stays as it is.')
    return "\n".join(lines)


_LINE = re.compile(r"(?m)^\s*(\d+)[.)]\s*(.*)$")


def replies(reply: str) -> dict[int, str]:
    """``{number: text}`` from a reply of numbered lines (a number's text runs to the next
    number)."""
    got: dict[int, str] = {}
    marks = list(_LINE.finditer(reply or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(reply)
        got[int(m.group(1))] = (m.group(2) + reply[m.end():end]).strip()
    return got


def _says(reply: str, word: str) -> bool:
    """The reply's first word is ``word`` (alone, or followed by punctuation and any
    explanation): the protocol's word, never text for the answer."""
    return re.match(rf"(?i){word}\s*(?:[.!:;,\u2014-]|$)", reply.strip()) is not None


def apply(text: str, patches: Sequence[Patch], reply: str) -> Applied:
    """``text`` with each patch's sentence swapped for the reply to its number. A reply for
    a number that is neither a sentence, ``stands`` nor ``drop`` replaces the sentence."""
    said = replies(reply)
    out = Applied(text)
    work = []
    for n, p in enumerate(patches, 1):
        got = said.get(n)
        if got is None or got == "":
            out.kept.append(n)
        elif _says(got, STANDS):
            out.stood += 1   # "stands. Both lines ..." is still "stands": none of it is spliced
        else:
            new = "" if got.lower().rstrip(".") == DROP else got
            work.append((p.start, p.end, new))
            out.changes.append({"n": n, "was": p.sentence, "now": new})
    for start, end, new in sorted(work, reverse=True):
        out.text = out.text[:start] + new + out.text[end:]
        if new:
            out.replaced += 1
        else:
            out.dropped += 1
    return out
