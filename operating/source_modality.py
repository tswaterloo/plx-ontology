"""The source's own modality of a passage an answer claims (the final-answer gate).

A document often states what is proposed, targeted, still to be approved, unverified or
only an example in the same words it uses for what is. A claim that quotes such a
passage passes the gate (the words are in the text), and an answer built on it then
states as fact what its source only proposes (SC-08 on v13: a clearing account "still
to approve" stated as where the cost sits; PS-A3: a deposit figure the proposal gives as
an example, "like" another order's, stated as the deposit; RRO-06: a target answered as
established). ``stated_modality(text, passage)`` reads the sentence of ``text`` that
holds ``passage`` and returns the modality words of ``MODALITY`` it finds there and
does not negate ("no longer pending approval", "not proposed"), in order and once; the
gate (``answer_gate.gate_answer``) then keeps the claim and adds one
unknown that names them (``MODALITY_UNKNOWN``), so the answer is partial and says the
source's own modality, never "is".

Deterministic, bounded, reads nothing; source text is compared, never interpreted.
"""

from __future__ import annotations

import re
from typing import Any

# (what the unknown calls it, the words that say it), in the order an unknown names them.
MODALITY = (
    ("proposed", r"\bpropos(?:ed|al|als|es|ing)\b"),
    ("still to be approved", r"\b(?:to be approved|still to approve|to approve|pending "
     r"(?:approval|sign-?off)|awaiting (?:approval|sign-?off)|not yet approved|unapproved|needs? (?:an? |the )?"
     r"approval|subject to approval|for approval)\b"),
    ("an open decision", r"\b(?:open (?:decision|question|item)s?|undecided|not (?:yet )?"
     r"decided|to be decided|tbd)\b"),
    ("a target", r"\b(?:target|to-be|future)[\s·:-]+(?:design|state|process|stages?|"
     r"flow|model|workflow)\b|^\W*target\b"),
    ("planned", r"\b(?:planned|plans? to)\b"),
    ("a draft", r"\bdraft\b"),
    ("unverified", r"\b(?:unverified|not (?:yet )?verified|to (?:be )?verif(?:y|ied)|not "
     r"(?:yet )?confirmed|unconfirmed|to be confirmed|tbc|assumed|an assumption)\b"),
    ("an example", r"\b(?:for example|for instance|e\.g\.|worked example|example)\b|\blike "
     r"[A-Z]{1,5}-?\d"),
    ("hedged", r"\b(?:possibly|probably|reportedly|allegedly|apparently|may have|might "
     r"have)\b"),
    ("indicative", r"\bindicative(?:ly)?\b"),
    ("estimated", r"\b(?:estimated?|estimates)\b"),
    ("tentative", r"\btentative(?:ly)?\b"),
)
_MODALITY = tuple((name, re.compile(words, re.I)) for name, words in MODALITY)
# A modality word the sentence negates says the opposite ("no longer pending approval",
# "not proposed", "is not a draft", "has not been proposed"): a negator at most two words
# before it. A pattern that holds its own negator ("not yet approved") starts with it, so
# nothing negates it from before.
_NEGATED_BEFORE = re.compile(r"(?:\b(?:not|never|no\s+longer|no|nor|neither)|n't)"
                             r"\s+(?:[\w-]+\s+){0,2}$", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[*])")
MAX_TEXT_CHARS = 2_000_000


def _squeeze(text: str) -> str:
    """``text`` with each run of spaces and tabs one space and each line's ends trimmed
    (a line break kept, so a sentence never reaches into the next line)."""
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def sentence_of(text: Any, passage: Any) -> str | None:
    """The sentence of ``text`` (within its line) that holds the first occurrence of
    ``passage`` (whitespace and case aside), widened to every sentence the passage
    spans; None when ``text`` does not hold it."""
    if not isinstance(text, str) or not isinstance(passage, str) or not passage.strip() \
            or len(text) > MAX_TEXT_CHARS:
        return None
    flat = _squeeze(text)
    want = " ".join(passage.split()).lower()
    found = flat.lower().replace("\n", " ").find(want)
    if found < 0:
        return None
    start = flat.rfind("\n", 0, found) + 1
    end = flat.find("\n", found + len(want))
    end = len(flat) if end < 0 else end
    line = flat[start:end]
    low, high = found - start, found - start + len(want)
    cuts = [0] + [m.end() for m in _SENTENCE_END.finditer(line)] + [len(line) + 1]
    first = max(cut for cut in cuts if cut <= low)
    last = min(cut for cut in cuts if cut >= high)
    return line[first:last].strip()


def stated_modality(text: Any, passage: Any) -> list[str]:
    """The ``MODALITY`` names the sentence holding ``passage`` in ``text`` says
    (``sentence_of``), in ``MODALITY`` order; none when the text does not hold it."""
    sentence = sentence_of(text, passage)
    if sentence is None:
        return []
    return [name for name, words in _MODALITY
            if any(not _NEGATED_BEFORE.search(sentence, 0, found.start())
                   for found in words.finditer(sentence))]
