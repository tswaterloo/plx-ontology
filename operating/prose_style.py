"""The Workbench's house style for prose a reader sees: the rule appended to every prompt
whose reply is shown as prose (``PROSE_STYLE_RULE``), and the patterns the static-copy lint
fails on (``TIC_PATTERNS``, ``tics``).

Both name the same tics: sentences built to stage a finding instead of stating it
(negation-first reveals, significance designations, inanimate subjects doing a person's
work, deferred nouns, coy or thesis-shaped headers, aphoristic closers, straw-man
knockdowns, fragment cadence, dressed metaphors and label-first framing). The rule changes
wording only; it adds no fact and never overrides an answer, citation or gate rule."""

from __future__ import annotations

import re

PROSE_STYLE_RULE = (
    "Writing style for every sentence a person reads: say the fact directly, with a real "
    "subject (a person, a record, a system) and a plain verb. Do not write: a "
    "negation-first reveal (\"It is not X. It is Y.\"; write \"It is Y\", or \"Y rather "
    "than X\" in one clause); a significance designation (\"the real question\", \"the "
    "part that matters\", \"here is the thing\"); an abstraction doing a person's work "
    "(\"the data shows\", \"the median hides it\"); a deferred noun (\"The sixth is not.\"; "
    "name it with its number); a coy or verdict-shaped heading (\"What this means\", \"Why "
    "it matters\"; use a plain noun phrase); a closing aphorism or moral that restates "
    "the paragraph; a straw man (\"you might think X, but\"); runs of short fragments for "
    "rhythm; metaphors (\"load-bearing\", \"the seam where\", \"north star\"); or a label "
    "before the content (\"Bottom line:\", \"Key takeaway:\", \"TL;DR\"). End on the last "
    "fact. This changes only your wording; every other rule above stays as it is.")

# Each pattern names one tic; a match in static copy is a defect. The patterns are narrow on
# purpose: plain copy such as "Not on record" or "Draft, not sent" never matches.
TIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
        ("negation-first reveal",
         r"\b(?:is|was|are|were)(?:n['’]t| not)\b[^.!?]{0,60}[.!?]\s+(?:it|this|that|they)"
         r"(?:['’]s| is| was| are| were)\b"),
        ("negation-first reveal", r"\b(?:isn['’]t|is not|wasn['’]t|was not) just\b"),
        ("negation-first reveal", r"\b(?:this|that)(?: is|['’]s) not the same as\b"),
        ("significance designation",
         r"\bthe (?:real|actual) (?:question|problem|issue|story|answer|point|work)\b"),
        ("significance designation",
         r"\bthe (?:part|thing|detail|bit|number) that (?:matters|counts|answers)\b"),
        ("significance designation",
         r"\bhere['’]s (?:the|what) (?:thing|catch|twist|kicker|rub|matters)\b"),
        ("significance designation", r"\bthe leg that\b"),
        ("abstraction agency",
         r"(?:^|[.!?:;]\s+)(?:the )?(?:table|data|median|chart|numbers?|metrics?|picture)\s+"
         r"(?:shows|hides|tells|says|reveals|lies)\s+(?:it|us|you|the story)\b"),
        ("deferred noun",
         r"[.!?]\s+the (?:first|second|third|fourth|fifth|sixth|last|other) (?:is|was) not\b"),
        ("deferred noun", r"\bone thing (?:is|was) not\b"),
        ("coy header", r"^(?:what|why|how) (?:it|this|that) (?:means|matters|works)\b"),
        ("coy header", r"^(?:what|how) we know\b"),
        ("coy header", r"^what (?:the data|it all) (?:actually )?(?:shows|means)\b"),
        ("aphoristic closer",
         r"\b(?:that|this)['’]?s? (?:is )?(?:the whole point|the point|the trick|"
         r"the difference|what matters)\s*[.!]"),
        ("aphoristic closer", r"\bnothing is made up in (?:its|their) place\b"),
        ("straw-man knockdown",
         r"\b(?:you might|you may|one might) (?:think|assume|expect)\b"),
        ("straw-man knockdown", r"\bis the obvious reading\b"),
        ("straw-man knockdown", r"\bit['’]s tempting to\b"),
        ("fragment cadence", r"(?:^|[.!?]\s)(?:\w+[.]\s){3,}"),
        ("dressed metaphor",
         r"\b(?:load-bearing|north star|tapestry|the seam where|fault line|deep dive|"
         r"game[- ]changer|supercharge|delve|single source of truth|beating heart)\b"),
        ("label-first framing",
         r"^(?:bottom line|tl;dr|key takeaway|the short version|the upshot|verdict|"
         r"the gist|in short)\s*[:—-]"),
    ))


def tics(text: str) -> list[str]:
    """The names of the tics ``text`` carries (empty when it is plain), in pattern order,
    each once. Header patterns (anchored at ^) test the text as a whole, so pass one
    string of copy at a time."""
    found: list[str] = []
    for name, pattern in TIC_PATTERNS:
        if name not in found and pattern.search(text.strip()):
            found.append(name)
    return found


__all__ = ["PROSE_STYLE_RULE", "TIC_PATTERNS", "tics"]
