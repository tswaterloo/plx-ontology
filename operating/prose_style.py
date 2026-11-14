"""The Workbench's house style for prose a reader sees, in one place: the rule every
generation prompt carries (``style_rule``; ``PROSE_STYLE_RULE`` is the house part), the
per-person layer a learned writing style adds on top (``set_person_style``), the fast
checker run on what a model wrote (``check``) and the patterns the static-copy lint fails
on (``TIC_PATTERNS``, ``tics``).

The rules distil two references, read as data: the declauding skill (oaustegard/
claude-skills, declauding v0.13.0: SKILL.md and references/register.md) and blader/humanizer
(SKILL.md). Both name one move behind most tics: a sentence built to make the reader feel a
finding arrive instead of stating it (negation-first reveals, significance designations,
inanimate subjects doing a person's work, deferred nouns, coy or verdict headers, aphoristic
closers, straw men, fragment cadence, dressed metaphors, label-first framing), plus the
chatbot defaults (residue, filler, inflated significance, participle tails, copula
avoidance, stock vocabulary). The rule changes wording only; it adds no fact and never
overrides an answer, citation or gate rule. ``operating.humanize`` repairs what ``check``
flags and proves every fact survived."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

PROSE_STYLE_RULE = (
    "Writing style for every sentence a person reads: write the way a colleague who knows "
    "the records would, stating each fact directly with a real subject (a person, a "
    "record, a company, a system) and a plain verb (is, has, sent, shipped). Keep "
    "sentences of mixed length, short for facts and longer for a cause or a caveat. "
    "Do not write: a negation-first reveal (\"It is not a delay. It is a cancellation.\"; "
    "write \"It is a cancellation\", or \"a cancellation rather than a delay\" in one "
    "clause); a significance designation (\"the real question\", \"the part that "
    "matters\", \"here is the thing\", \"notably\"); an abstraction doing a person's "
    "work (\"the data shows\", \"the timeline tells us\"); a deferred noun (\"The "
    "sixth is not.\"; name it with its number); a coy or verdict heading (\"What this "
    "means\", \"Why it matters\", \"The short answer\"; use a plain noun phrase or "
    "none); a label before the content (\"Bottom line:\", \"Key takeaway:\", "
    "\"Short answer:\", \"TL;DR\"); a straw man (\"you might think X, but\"); a "
    "rhetorical question you answer yourself; runs of short fragments for rhythm; a "
    "closing moral or summary that restates the paragraph (end on the last fact); "
    "metaphors standing in for a mechanism (\"load-bearing\", \"the seam where\", "
    "\"north star\", \"perfect storm\"); inflated significance (\"plays a key role\", "
    "\"a testament to\", \"underscores\", \"pivotal\"); a trailing -ing clause that "
    "comments on the sentence (\", highlighting the risk\"); \"serves as\" or "
    "\"stands as\" where \"is\" fits; stock words (delve, seamless, robust, leverage, "
    "utilize, crucial, comprehensive, streamline, landscape); filler (\"in order to\", "
    "\"it is important to note\", \"due to the fact that\"); a bare adverb in place of "
    "evidence (\"quietly\", \"plainly\", \"genuinely\"); announced candour (\"to be "
    "clear\", \"honestly\"); \"it turns out\"; em dashes as the all-purpose joint (use "
    "a comma, a colon or two sentences); bold words mid-sentence; emoji; or chatbot "
    "wrapping (\"Great question\", \"I hope this helps\", \"Let me know if you'd "
    "like\", \"Here is an overview of\"). Keep every hedge the evidence needs and "
    "every specific detail; plain is not timid, and shorter is not plainer. This changes "
    "only your wording; every other rule above stays as it is.")

# Added for a message sent as a person (an email or Teams draft): it reads as that person
# wrote it to a colleague or a supplier they know.
DRAFT_STYLE_RULE = (
    "A message sent in a person's name reads as they wrote it: a greeting only if they "
    "use one, then the point in the first sentence, the facts the reader needs, and what "
    "you need from them. No \"I hope this email finds you well\", \"I wanted to reach "
    "out\", \"just circling back\", \"please don't hesitate\", \"kindly\" or "
    "\"thank you for your patience\"; no headings or bullet labels in a short note.")

# The register a person's own writing wins over (the declauding skill's "author's sample"
# rule): a learned style lists its rules here, layered after the house style, and may
# allow tics its owner really uses (``PersonStyle.allow``: names from ``FAMILIES``).
PERSON_HEADING = ("How {who} writes, learned from their own edits (where it differs from "
                  "the style above, follow this):")


@dataclass(frozen=True)
class PersonStyle:
    """One person's learned writing style: ``rules`` (short imperative lines, at most
    ``MAX_PERSON_RULES`` used, each at most ``MAX_RULE_CHARS``) and ``allow`` (tic families
    the checker does not flag in their drafts, e.g. ``em dash`` for a person who writes
    them)."""
    rules: tuple[str, ...] = ()
    allow: frozenset[str] = field(default_factory=frozenset)


MAX_PERSON_RULES = 12
MAX_RULE_CHARS = 200
_PROVIDER_LOCK = threading.Lock()
_provider: Optional[Callable[[str], Any]] = None


def set_person_style(provider: Optional[Callable[[str], Any]]) -> None:
    """The hook a learned per-person style registers: ``provider(person)``
    with the person's lower-case email address returns a ``PersonStyle``, a mapping
    ``{"rules": [...], "allow": [...]}`` or None. None unregisters. Only the person a draft
    is sent as is ever asked; a provider that raises or returns anything else counts as no
    style (house style only)."""
    global _provider
    with _PROVIDER_LOCK:
        _provider = provider


def person_style(person: Optional[str]) -> Optional[PersonStyle]:
    """``person``'s learned style through the registered hook, validated; None when there
    is no person, no hook, or the hook fails or answers with something unknown."""
    provider = _provider
    if not person or provider is None:
        return None
    try:
        got = provider(str(person).strip().lower())
    except Exception:  # noqa: BLE001 - a broken style hook never blocks a draft
        logging.getLogger("operating.prose_style").warning("person style hook failed")
        return None
    if isinstance(got, PersonStyle):
        rules, allow = got.rules, got.allow
    elif isinstance(got, Mapping):
        rules, allow = got.get("rules") or (), got.get("allow") or ()
    else:
        return None
    if isinstance(rules, str) or isinstance(allow, str):
        return None
    clean = tuple(" ".join(str(r).split())[:MAX_RULE_CHARS] for r in rules
                  if isinstance(r, str) and r.strip())[:MAX_PERSON_RULES]
    allowed = frozenset(a for a in allow if a in FAMILIES)
    return PersonStyle(clean, allowed) if clean or allowed else None


def style_rule(person: Optional[str] = None, *, draft: bool = False,
               who: Optional[str] = None, learned: Any = None) -> str:
    """The style text a generation prompt ends with: the house rule, the message rule when
    the text is sent as a person (``draft``), then that person's learned rules (``person``:
    their email address; ``who``: how the heading names them)."""
    parts = [PROSE_STYLE_RULE]
    if draft:
        parts.append(DRAFT_STYLE_RULE)
    # ``learned``: the person's style already read (False: read, none), so the hook is
    # asked once per text
    style = (learned or None) if learned is not None else (
        person_style(person) if person else None)
    if style is not None and style.rules:
        parts.append(PERSON_HEADING.format(who=who or "this person") + "\n"
                     + "\n".join(f"- {r}" for r in style.rules))
    return "\n".join(parts)


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




# ---- the checker for generated text ---------------------------------------------------
#
# ``check`` reads what a model wrote (an answer, a report, a draft) and names the tics in it.
# Each pattern has a family and a weight: a ``strong`` tic is a staged or chatbot shape that
# is a tic wherever it stands; a ``weak`` one (a stock word, an em dash, a bold label) is a
# tic only in a cluster, so one alone never triggers a rewrite (the references: "look for
# clusters"). Patterns are compiled once; a 2,000-character answer checks in well under a
# millisecond.

KINDS = ("answer", "report", "draft")

_S = r"(?:^|[.!?:;]\s+|\n\s*)"               # a sentence (or line) start
_CAP_PRONOUN = r"(?:it|this|that|the (?:problem|issue|question|answer|point|cause|delay|" \
    r"risk|gap|hold-?up|reason|catch))"

# (family, weight, pattern, kinds or None for all)
_GENERATED: tuple[tuple[str, str, str, Optional[tuple[str, ...]]], ...] = (
    # staging
    ("negation-first reveal", "strong",
     _S + _CAP_PRONOUN + r"(?:['’]s| is| was)(?:n['’]t| not)\b[^.!?\n]{0,60}[.!?]\s+"
     r"(?:it|this|that)(?:['’]s| is| was)\b", None),
    ("negation-first reveal", "strong",
     r"\b(?:it|this|that)(?:['’]s| is| was)(?:n['’]t| not) (?:about |that |because |just )?"
     r"[^.;!?\n]{1,60}[,;—–]\s*(?:it|this|that)(?:['’]s| is| was)\b", None),
    ("negation-first reveal", "strong", r"\bnot because\b[^.;!?\n]{1,80}\bbut because\b",
     None),
    ("negation-first reveal", "strong",
     r"\b(?:isn['’]t|is not|wasn['’]t|was not|doesn['’]t|does not|didn['’]t|did not) "
     r"just\b[^.;!?\n]{1,60}[,;—–]\s*(?:it|they|but)\b", None),
    ("negation-first reveal", "strong", r"\b(?:this|that)(?: is|['’]s) not the same as\b",
     None),
    # opener forms (deletable by a rewrite: they only introduce the statement)
    ("significance designation", "strong",
     _S + r"the (?:real|actual|key|crucial|important|interesting|big) (?:question|problem|"
     r"issue|story|point|takeaway|thing|catch|twist) (?:is|was|here is)"
     # introducing a clause, never a subject ("The real problem was fixed on 11-10")
     r"(?=\s*:|\s+(?:that|whether|when|how|why|what|who|where)\b)", None),
    ("significance designation", "strong",
     _S + r"here['’]s (?:the|what) (?:thing|catch|twist|kicker|rub|matters|happened|i found)\b",
     None),
    ("significance designation", "strong",
     _S + r"what (?:really |actually )?matters (?:here |most )?is"
     r"(?=\s*:|\s+(?:that|whether|when|how|why|what|who|where)\b)", None),
    # noun-phrase forms: flagged, never deleted ("replace the part that counts the pallets")
    ("significance phrase", "strong",
     r"\bthe (?:real|actual|key|crucial|important|interesting|big) (?:question|problem|issue|"
     r"story|point|takeaway|thing|catch|twist)\b", None),
    ("significance phrase", "strong",
     r"\bthe (?:part|thing|detail|bit|number|piece) that (?:matters|counts|answers|stands out)"
     r"\b", None),
    ("significance phrase", "strong", r"\b(?:and )?that['’]s the (?:interesting|important|"
     r"key|crucial) (?:part|bit)\b", None),
    ("significance phrase", "strong", r"\bone thing stands out\b", None),
    ("abstraction agency", "strong",
     r"\b(?:the )?(?:data|numbers|timeline|picture|metrics?|median|chart)\s+"
     r"(?:shows|hides|tells|says|reveals|lies)\s+(?:it|us|you|the story)\b", None),
    ("deferred noun", "strong",
     r"[.!?]\s+the (?:first|second|third|fourth|fifth|sixth|last|other) (?:is|was) not\b",
     None),
    ("deferred noun", "strong",
     r"\bthere(?:['’]s| is| was) (?:just )?one (?:problem|catch|thing|snag|issue)\b", None),
    ("straw man", "strong", r"\b(?:you might|you may|one might) (?:think|assume|expect)\b",
     None),
    ("straw man", "strong", r"\bit['’]s tempting to\b", None),
    ("straw man", "strong", r"\bis the obvious reading\b", None),
    ("rhetorical question", "strong",
     _S + r"(?:so )?(?:why|what|how)\b[^?\n]{0,60}\?\s+(?:because|simple|easy|short answer|"
     r"mostly|it['’]s|the answer)\b", None),
    ("aphoristic closer", "strong",
     r"\b(?:that|this)(?:['’]s| is) (?:the whole point|the point|the trick|the difference|"
     r"what matters|exactly the point|the whole story)\s*[.!]", None),
    ("aphoristic closer", "strong",
     r"\b(?:and )?that makes all the difference\b|\bwhich says it all\b", None),
    # a sentence opener only: "due at the end of the day" is a deadline
    ("aphoristic closer", "strong", _S + r"at the end of the day,", None),
    ("self-grading", "strong",
     r"\b(?:it['’]s|it is) (?:also )?worth (?:noting|mentioning|pointing out|flagging)\b|"
     r"\bworth noting\b|\bit (?:is|should be) (?:important to note|noted)\b", None),
    ("announced candour", "strong",
     _S + r"(?:honestly|frankly|look|to be clear|to be fair|to be honest|let['’]s be honest|"
     r"i['’]ll be honest),", None),
    ("it turns out", "strong", _S + r"(?:it )?turns out\b", None),
    ("label-first", "strong",
     r"(?:^|\n|[.!?]\s)\s*(?:\*\*|#+\s*)?(?:short answer|long answer|bottom line|tl;?dr|"
     r"(?:key |the )?takeaways?|the upshot|the gist|in short|verdict|net net)(?:\*\*)?"
     r"(?:\s*:|\s+[—–-]\s)(?:\*\*)?", None),     # a colon or a spaced dash: never "short-term"
    ("coy header", "strong",
     r"(?:^|\n)\s*(?:#+\s*|\*\*)(?:what|why|how) (?:it|this|that) (?:means|matters|works)\b",
     None),
    ("coy header", "strong",
     r"(?:^|\n)\s*(?:#+\s*|\*\*)(?:the )?(?:short version|big picture|verdict|catch|"
     r"upshot|bottom line)\b", None),
    ("inflated significance", "strong",
     r"\b(?:a |an )?testament to\b|\bplays? an? (?:key|crucial|pivotal|vital|important|"
     r"significant|central) role\b|\bsetting the stage\b|\b(?:evolving|ever-changing) "
     r"landscape\b|\bunderscor(?:es|ing|ed) (?:the|its|their|how|that)\b|\bhighlight(?:s|ing) "
     r"the importance\b|\b(?:enduring|lasting) legacy\b|\bindelible\b|\bnavigat(?:e|ing) the "
     r"complexit", None),
    ("participle tail", "strong",
     r",\s+(?:highlighting|underscoring|emphasi[sz]ing|showcasing|reflecting|signall?ing|"
     r"symboli[sz]ing|fostering|cementing|solidifying|illustrating)\b", None),
    ("copula avoidance", "strong",
     r"\b(?:serves|stands|functions) as (?:a|an|the)\b|\bboasts\b", None),
    ("dressed metaphor", "strong",
     r"\b(?:load-bearing|north star|tapestry|the seam where|fault line|deep dive|"
     r"game[- ]changer|supercharge|beating heart|perfect storm|tip of the iceberg|"
     r"double-edged sword|silver bullet|low-hanging fruit|move the needle|rabbit hole|"
     r"under the hood|paints? a (?:clear |mixed |different )?picture|in [a-z]+['’]s "
     r"clothing)\b", None),
    ("chatbot residue", "strong",
     r"\b(?:great|good|excellent) question\b|\bi hope (?:this|that) helps\b|"
     r"\bwould you like me to\b|\b(?:happy|glad) to help\b|\bas an ai\b|"
     r"\bhere(?:['’]s| is) (?:a |an )?(?:quick |brief |short )?(?:overview|summary|"
     r"breakdown|rundown) of\b|\blet me (?:explain|break (?:this|it) down|walk you through|"
     r"dig into)\b|\bin this (?:answer|reply|response|summary),? i(?:['’]ll| will)\b", None),
    ("chatbot residue", "strong",
     r"\b(?:let me know|tell me) if you(?:['’]d| would) like\b|"
     r"(?:^|\n)\s*(?:certainly|of course|absolutely|sure thing)[!,.]",
     ("answer", "report")),
    # an offer takes in its own content: flagged, never deleted by a model rewrite
    ("chatbot offer", "strong", r"\bwant me to\b[^.?\n]{0,60}\?", ("answer", "report")),
    ("knowledge-limit", "strong",
     r"\bas of my (?:last|latest) (?:update|knowledge)\b|\bbased on (?:the )?available "
     r"information\b|\bwhile (?:specific )?details are limited\b", None),
    ("fragment cadence", "strong", r"(?:^|[.!?]\s)(?:\w+[.]\s){3,}", None),
    ("emoji", "strong", "[\U0001F300-\U0001FAFF☀-➿⭐✅❌]", None),
    # weak alone: a tic only in a cluster
    ("stock word", "weak",
     r"\b(?:delv(?:e|es|ing)|myriad|plethora|realm|seamless(?:ly)?|utili[sz](?:e|es|ed|ing)|"
     r"leverag(?:e|es|ed|ing)(?= (?:the|our|this|these|its|their|existing))|elevat(?:e|es|ing)|"
     r"unlock(?:s|ing)?|harness(?:es|ing)?|embark(?:s|ing)?|intricate|interplay|"
     r"meticulous(?:ly)?|vibrant|showcas(?:e|es|ing)|garner(?:s|ed)?|bolster(?:s|ed)?|"
     r"pivotal|crucial|comprehensive|holistic|streamlin(?:e|es|ed|ing)|cutting-edge|synergy|"
     r"paradigm|robust|empower(?:s|ed|ing)?)\b", None),
    ("stock transition", "weak",
     _S + r"(?:additionally|furthermore|moreover|notably|importantly|crucially|interestingly|"
     r"ultimately|in summary|in conclusion|to summari[sz]e|overall),", None),
    ("flat-certainty adverb", "weak",
     r"\b(?:quietly|plainly|genuinely|demonstrably|provably|outright|deliberately)\b", None),
    ("filler", "weak",
     r"\bin order to\b|\bdue to the fact that\b|\bat this point in time\b|\bin the event "
     r"that\b|\bhas the ability to\b|\bfor all intents and purposes\b", None),
    ("em dash", "weak", "—", None),
    ("bold label", "weak", r"(?:^|\n)\s*(?:[-*•]|\d+\.)\s+\*\*[^*\n]{1,40}:?\*\*:?", None),
    ("ensuring tail", "weak", r",\s+(?:ensuring|contributing to|encompassing)\b", None),
)

FAMILIES = frozenset(f for f, _, _, _ in _GENERATED) | {"stock phrase"}
# matched against the lower-cased text (no IGNORECASE: several times faster)
_COMPILED = tuple((family, weight, re.compile(pattern), kinds)
                  for family, weight, pattern, kinds in _GENERATED)
# weak hits that make a cluster: this many in one text (or one per 60 words, the larger)
CLUSTER = 3


def _draft_phrases() -> tuple[re.Pattern[str], ...]:
    """The copy rules' banned phrases for an outgoing message (``copy_rules.json``, the
    copy gate's own list): in a model-written draft each is a strong tic."""
    if not hasattr(_draft_phrases, "_cache"):
        path = Path(__file__).with_name("copy_rules.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("banned_phrases") or ()
        except (OSError, ValueError):
            raw = ()
        _draft_phrases._cache = tuple(  # type: ignore[attr-defined]
            re.compile(r"\b" + r["pattern"].lower() + r"\b") for r in raw
            if isinstance(r, Mapping) and "draft" in (r.get("applies") or ())
            and isinstance(r.get("pattern"), str)
            # a sign-off is the sender's own (copy gate's voice check, a learned style)
            and "sign-off" not in str(r.get("reason") or ""))
    return _draft_phrases._cache  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Hit:
    family: str
    weight: str          # strong | weak
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Report:
    hits: tuple[Hit, ...]
    words: int

    @property
    def strong(self) -> tuple[Hit, ...]:
        return tuple(h for h in self.hits if h.weight == "strong")

    @property
    def weak(self) -> tuple[Hit, ...]:
        return tuple(h for h in self.hits if h.weight == "weak")

    @property
    def flagged(self) -> bool:
        """Whether the text needs a repair: a strong tic, or a cluster of weak ones."""
        return bool(self.strong) or len(self.weak) >= max(CLUSTER, self.words // 60)

    def families(self) -> list[str]:
        return list(dict.fromkeys(h.family for h in self.hits))


_QUOTED = re.compile(r"\"[^\"\n]{1,400}\"|“[^”\n]{1,400}”|`[^`\n]{1,400}`|^>.*$", re.M)


def check(text: str, *, kind: str = "answer", allow: Iterable[str] = ()) -> Report:
    """The tics in ``text`` (a model-written ``kind``: ``KINDS``). Quoted passages (a
    source's own words in quotes, code, a ``>`` quote) are never flagged: they are
    evidence, not the writer's voice. ``allow``: families a person's own style uses."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    text = text or ""
    low = text.lower()          # same length and offsets as ``text``
    if len(low) != len(text):
        low = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
    allowed = set(allow)
    quoted = [(m.start(), m.end()) for m in _QUOTED.finditer(text)]

    def inside(a: int, b: int) -> bool:
        return any(s <= a and b <= e for s, e in quoted)

    hits: list[Hit] = []
    for family, weight, rx, kinds in _COMPILED:
        if family in allowed or (kinds is not None and kind not in kinds):
            continue
        for m in rx.finditer(low):
            # a hit starts at its first word, never at the end of the sentence before it
            start = m.start()
            while start < m.end() and low[start] in ".!?:;\n \t":
                start += 1
            if m.end() > start and not inside(start, m.end()):
                hits.append(Hit(family, weight, start, m.end(), text[start:m.end()]))
    if kind == "draft" and "stock phrase" not in allowed:
        for rx in _draft_phrases():
            for m in rx.finditer(low):
                if not inside(m.start(), m.end()):
                    hits.append(Hit("stock phrase", "strong", m.start(), m.end(),
                                    text[m.start():m.end()]))
    hits.sort(key=lambda h: (h.start, h.end))
    return Report(tuple(hits), len(re.findall(r"[A-Za-z0-9'’]+", text)))


def per_thousand(reports: Sequence[Report]) -> float:
    """Tics per 1,000 words over ``reports``."""
    words = sum(r.words for r in reports)
    return round(1000.0 * sum(len(r.hits) for r in reports) / words, 2) if words else 0.0


__all__ = ["CLUSTER", "DRAFT_STYLE_RULE", "FAMILIES", "Hit", "KINDS", "PROSE_STYLE_RULE",
           "PersonStyle", "Report", "TIC_PATTERNS", "check", "per_thousand", "person_style",
           "set_person_style", "style_rule", "tics"]
