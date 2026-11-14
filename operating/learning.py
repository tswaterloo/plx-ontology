"""Learning: what people do with what the Workbench gave them becomes lessons, applied the
next time it drafts for them, answers them or alerts them.

**Signals** (``Signal``; collected by ``operating.learning_signals``): a prepared draft the
person answered in the thread (``draft_sent``: what their reply kept, changed, added and
dropped of the draft), a prepared item they left (``draft_ignored``, past due and the
judging horizon with no action), an Ask thumbs (``answer_rated``: the systems the answer
cited), a right answer given ("the right answer is", or "no, it's X" after an answer:
``answer_corrected``), a question asked again in other words soon after an answer
(``reasked``: the first answer did not serve) and an alert marked useful, not useful or
wrong (``alert_verdict``). A signal keeps features and references, never a message's
text: a greeting form, a sign-off, a word count, generic sentence templates (no digit,
address, amount or name), digests of a sender and of topic words, and the ids of the
draft item and the reply it is paired with (the reply's words stay in the capture
store and are read from there when an example is shown).

**Lessons** (``distil``): per person and key (``style``: greeting, sign-off, length,
phrases added or dropped; ``fact``: a corrected answer; ``source``: systems whose answers
were rated up or down; ``decision``: items of one kind, channel and side, or one sender,
the person acts on or leaves; ``detector``: alert kinds they dismiss), each value's
evidence decays with its age (``HALF_LIFE`` days per kind) and the lesson's confidence
is the winning value's weight over all weight plus ``PRIOR``: it grows with repetition,
falls with contradiction and fades when not seen again. Every lesson keeps its evidence
(signal references and times). **Role lessons** come only from categorical, non-private
patterns (greeting, sign-off, length, sources, alert kinds, item kinds) that at least
``ROLE_MIN_PEOPLE`` members of the role learned with the same value; never a fact, a
phrase or a sender. A role lesson applies only where the person has none of their own.

**Applied** (``Lessons``): ``apply_draft`` re-frames a prepared draft in the person's
learned greeting and sign-off, drops sentences they always delete and adds the closing
sentence they nearly always add (only sentences with no figure, so a cited fact is never
touched; an added sentence passes the copy gate first) and names the lessons and the
person's own past draft-to-reply pairs most like this message (``examples``);
``skip_reason`` leaves out an item of a kind (or from a sender) the person has left at
least ``SKIP_MIN`` times with confidence ``SKIP_AT`` or more; ``alert_weight`` lowers the
rank of an alert kind a reader dismisses; ``memory_items`` are the person's lessons as
``operator_api.memory`` preferences and facts (the Ask reads them from there).

**Privacy.** The people exclusion is read first and fails closed: unavailable, nothing is
taken in and nothing distilled (``Blocked``); a signal whose person or counterpart it
does not clear is dropped. Pay and finance never become a lesson: a signal whose text or
template carries an amount, a finance or pay word or personal financial content is
dropped (``finance_text``), and an answer served under a finance or PII marking teaches
no source and no fact. Lessons are per person (a reader's key); a forgotten lesson
(``forget``) is never learned again.

Ledger: ``<state>/learning/learning.sqlite`` (``PLX_LEARNING_DIR`` overrides), 0600 in a
0700 folder. ``PLX_LEARNING_OFF=1`` applies nothing.

    python3 -m operating.learning distil --state-dir DIR [--store STORE]
    python3 -m operating.learning show --state-dir DIR --person ID
    python3 -m operating.learning forget --state-dir DIR --person ID --kind K --key KEY
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

SCHEMA = "plx-learning/v1"
DIR_ENV = "PLX_LEARNING_DIR"
OFF_ENV = "PLX_LEARNING_OFF"
FILE = "learning.sqlite"

SIGNAL_KINDS = ("draft_sent", "replied", "item_acted", "draft_ignored", "answer_rated",
                "answer_corrected", "reasked", "alert_verdict")
LESSON_KINDS = ("style", "fact", "source", "decision", "detector")
# days for one observation's weight to halve, per lesson kind
HALF_LIFE = {"style": 120.0, "fact": 60.0, "source": 90.0, "decision": 45.0,
             "detector": 30.0}
PRIOR = 1.5           # weight of "not known yet": one observation gives 0.4
APPLY_AT = 0.55       # a lesson shapes a draft, an answer or an alert from here
SKIP_AT = 0.6         # an item kind (or sender) the person leaves is left out from here ...
SKIP_MIN = 3.0        # ... and only after this much (decayed) evidence (tuned 11-13 on 07-08..11-05)
DECISION_PRIOR = PRIOR  # "not known yet" for an act/leave decision
ROLE_MIN_PEOPLE = 2
EVIDENCE_KEPT = 20
EXAMPLES = 3
SHAPE_ENDS = 2       # a reply's first and last sentences kept as templates (few-shot)
# role lessons: categorical keys only (never a fact, a phrase or a sender)
ROLE_KEYS = re.compile(r"^(?:greeting|signoff|length|source:[a-z_]+|detector:[a-z_]+|"
                       r"decision:[a-z_]+\|[a-z_]+\|[a-z_]+)$")

GREETINGS = ("hi_name", "hello_name", "hey_name", "dear_name", "good_morning_name",
             "name_only", "hi", "hello", "good_morning", "none")
SIGNOFFS = ("none", "thanks", "thank_you", "regards", "best_regards", "kind_regards",
            "best", "cheers")
SIGNOFF_TEXT = {"thanks": "Thanks", "thank_you": "Thank you", "regards": "Regards",
                "best_regards": "Best regards", "kind_regards": "Kind regards",
                "best": "Best", "cheers": "Cheers"}
_GREET = re.compile(r"^\s*(hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))"
                    r"(?:\s+([A-Z][A-Za-z'\-]{1,30}))?\s*[,!.:\-]?\s*$", re.I)
_NAME_ONLY = re.compile(r"^\s*([A-Z][a-z'\-]{1,30})\s*,\s*$")
_CLOSE = re.compile(r"^\s*(thanks(?: so much| again)?|thank you(?: so much)?|many thanks|"
                    r"regards|best regards|kind regards|warm regards|best|cheers)"
                    r"\s*[,!.]?\s*$", re.I)
_QUOTED = re.compile(r"\n\s*(?:From:|-{2,}\s*Original Message|On .{5,80} wrote:|"
                     r"_{5,}|Sent from my )", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_FIGURE = re.compile(r"\d")
_ADDRESS = re.compile(r"[\w.%+'-]+@[\w.-]+\.\w{2,}|https?://|www\.|"
                      r"\b[\w-]+\.(?:com|ca|net|org|io|co|us|uk)\b", re.I)
_MONEY = re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:usd|cad|eur|gbp|dollars?)\b", re.I)
_FINANCE = re.compile(
    r"\b(?:salary|salaries|payroll|pay\s?rate|pay\s?stub|wages?|bonus(?:es)?|"
    r"compensation|raise|benefits|invoice[sd]?|payments?|paid|pay|prices?|pricing|costs?|"
    r"credit|remittance|balance|overdue|refund|margin|budget|cash|bank|tax(?:es)?|"
    r"discount|quote[sd]?|amount|owed?|owing|ar|ap|sin|ssn)\b", re.I)
_CORRECTION = re.compile(
    r"^\s*(?:no[,.!]?\s+(?:it'?s|it is|the (?:right|correct) (?:answer|one) is|that'?s|"
    r"actually)|actually[,]?\s+(?:it'?s|it is)|wrong[,.!]?\s+(?:it'?s|it is)|"
    r"that'?s (?:wrong|not right)[,.!]?\s+(?:it'?s|it is))\s+(?P<value>.{1,300}?)\s*[.!]?\s*$",
    re.I)
_WORD = re.compile(r"[a-z][a-z'\-]{2,}")
# common first words of a generic sentence (a capitalised first word outside these and
# ``_STOP`` may be a name: such a sentence is never a template)
_OPENERS = frozenset("i i'm i'll i've i'd we we're we'll we've let see attached here is it "
                     "it's this that yes no ok okay sure great perfect noted sorry just "
                     "looking hope please thanks thank kindly could would can will do does "
                     "did have has had may might should as per also once if when unfortunately"
                     " apologies appreciate received got sounds good confirmed confirming "
                     "following following-up checking any all a an the in on for to so "
                     "my our your there these those what which how why where who "
                     "note fyi glad happy best regards cheers much many".split())
_STOP = frozenset("the and for you your are was were this that with have has from not but "
                  "can could would will what when where which who how our out any all "
                  "just about there their them they please thanks thank hi hello".split())


class Blocked(RuntimeError):
    """The people exclusion cannot decide: nothing is taken in or distilled."""


# ----------------------------------------------------------------------------- helpers

def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def instant(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        got = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:16]


def finance_text(text: Any) -> bool:
    """Whether ``text`` carries money, a finance or pay word or personal financial content
    (undecidable: yes, fail closed)."""
    if not isinstance(text, str) or not text:
        return False
    if _MONEY.search(text) or _FINANCE.search(text):
        return True
    try:
        from .chunking.sensitive import personal_financial
        return personal_financial(text) is not None
    except Exception:  # noqa: BLE001 - undecidable: treated as finance
        return True


def person_id(reader: Any) -> str:
    """One id per person across the stores: the Entra object id of an ``employee:<oid>``
    principal in a reader key (the autopilot's owner id), else the key itself."""
    text = str(reader or "").strip()
    found = re.search(r"(?:^|[,:])employee:([0-9A-Fa-f-]{36})(?:,|$)", text)
    if found:
        return found.group(1).lower()
    return text.lower() if re.fullmatch(r"[0-9a-fA-F-]{36}", text) else text


def topic_keys(text: Any, most: int = 24) -> list[str]:
    """Digests of a text's content words (what an example is matched on; never words)."""
    words = [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]
    return sorted({digest("w", w)[:8] for w in words})[:most]


# ----------------------------------------------------------------------------- text features

def reply_core(text: Any) -> str:
    """A reply without the quoted thread below it."""
    text = str(text or "").replace("\r\n", "\n")
    cut = _QUOTED.search(text)
    return (text[:cut.start()] if cut else text).strip()


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def greeting_of(text: str) -> tuple[str, Optional[str]]:
    """(the greeting form of a message's first line, the name it greets) — "none" when the
    first line is no greeting."""
    lines = _lines(text)
    if not lines:
        return "none", None
    first = lines[0]
    found = _GREET.match(first)
    if found:
        word = re.sub(r"\s+", "_", found.group(1).lower())
        name = found.group(2)
        if word == "dear" and not name:
            return "none", None
        form = {"hi": "hi", "hello": "hello", "hey": "hi", "good_morning": "good_morning",
                "good_afternoon": "good_morning", "good_evening": "good_morning",
                "dear": "dear"}[word]
        if name:
            return ({"hi": "hey_name" if word == "hey" else "hi_name", "hello": "hello_name",
                     "good_morning": "good_morning_name", "dear": "dear_name"}[form], name)
        return form, None
    found = _NAME_ONLY.match(first)
    if found:
        return "name_only", found.group(1)
    return "none", None


def signoff_of(text: str) -> tuple[str, Optional[str]]:
    """(the sign-off form among the message's last lines, its literal words) — "none"
    when no closing line ends the message before the signature."""
    lines = _lines(text)
    for line in reversed(lines[-6:]):
        found = _CLOSE.match(line)
        if found:
            words = found.group(1).lower()
            form = ("thanks" if words.startswith(("thanks", "many thanks"))
                    else "thank_you" if words.startswith("thank you")
                    else "best_regards" if words == "best regards"
                    else "kind_regards" if words in ("kind regards", "warm regards")
                    else words if words in SIGNOFFS else "regards")
            return form, line.strip()
    return "none", None


def body_sentences(text: str) -> list[str]:
    """The sentences between the greeting and the sign-off."""
    lines = _lines(text)
    if lines and greeting_of(lines[0])[0] != "none":
        lines = lines[1:]
    out: list[str] = []
    for line in lines:
        if _CLOSE.match(line):
            break
        out.extend(s.strip() for s in _SENTENCE.findall(line.lstrip("-* ")) if s.strip())
    return out


def template(sentence: str) -> Optional[str]:
    """A sentence as a generic template: lower-cased, spaced; None when it carries a
    figure, an address, a link, money, a finance word or a capitalised name inside it
    (anything that is a fact or a person, never a style)."""
    text = sentence.strip()
    if not 8 <= len(text) <= 160 or _FIGURE.search(text) or _ADDRESS.search(text) \
            or finance_text(text):
        return None
    words = text.split()
    if any(w[:1].isupper() and w.strip(".,!?;:").isalpha() and w.lower() not in ("i",)
           for w in words[1:]):
        return None
    lead = words[0].strip(".,!?;:'\"").lower() if words else ""
    if words and words[0][:1].isupper() and lead not in _STOP and lead not in _OPENERS:
        return None     # a capitalised first word that is no common opener: maybe a name
    return re.sub(r"\s+", " ", text.lower()).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def edit_features(draft: str, sent: str) -> dict[str, Any]:
    """What a person's reply kept, changed, added and dropped of a draft, as features
    (forms, counts and generic templates; never the reply's own words otherwise)."""
    sent = reply_core(sent)
    greet, _name = greeting_of(sent)
    close, literal = signoff_of(sent)
    said = body_sentences(sent)
    drafted = body_sentences(draft)
    added = [t for s in said if not any(_similar(s, d) >= 0.6 for d in drafted)
             and (t := template(s))]
    dropped = [t for d in drafted if not any(_similar(d, s) >= 0.6 for s in said)
               and (t := template(d))]
    kept = [t for d in drafted if any(_similar(d, s) >= 0.6 for s in said)
            and (t := template(d))]
    # the reply's opening and closing sentences (``SHAPE_ENDS`` each) as generic templates,
    # "#" for one with a fact and for every sentence between them (never kept)
    shape = [(template(s) or "#") if i < SHAPE_ENDS or i >= len(said) - SHAPE_ENDS else "#"
             for i, s in enumerate(said)][:12]
    return {"greeting": greet, "signoff": close,
            "signoff_text": literal if literal and template(literal + ".") else None,
            "words": len(" ".join(said).split()), "shape": shape, "added": sorted(set(added))[:6],
            "dropped": sorted(set(dropped))[:6], "kept": sorted(set(kept))[:6]}


def correction_of(text: Any) -> Optional[str]:
    """The value of an explicit correction ("no, it's X", "actually it is X"), else None."""
    found = _CORRECTION.match(str(text or ""))
    return found.group("value").strip() if found else None


def rephrased(first: Any, second: Any) -> bool:
    """Whether ``second`` asks ``first`` again in other words (most content words shared,
    not the same text)."""
    a = set(topic_keys(first, 64))
    b = set(topic_keys(second, 64))
    if not a or not b or str(first).strip().lower() == str(second).strip().lower():
        return False
    return len(a & b) / len(a | b) >= 0.4


def normalized_edit_distance(a: str, b: str) -> float:
    """Word-level Levenshtein distance over the longer length (0 same, 1 all different)."""
    x, y = str(a or "").split(), str(b or "").split()
    if not x and not y:
        return 0.0
    prev = list(range(len(y) + 1))
    for i, wx in enumerate(x, 1):
        cur = [i] + [0] * len(y)
        for j, wy in enumerate(y, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (wx.lower() != wy.lower()))
        prev = cur
    return prev[-1] / max(len(x), len(y))


# ----------------------------------------------------------------------------- privacy guard

class _Unavailable:
    unavailable = "people_exclusion_unloadable"


_UNAVAILABLE = _Unavailable()


class Guard:
    """Who may be learned about: ``clear(identity)`` True only for a person the exclusion
    decides is not excluded (fail closed); ``available`` False: nothing is learned."""

    def __init__(self, exclusion: Any = None, *, clear: Optional[Callable[[str], bool]] = None
                 ) -> None:
        self._clear = clear
        self._ex = exclusion

    @classmethod
    def from_env(cls, people: Any = None) -> "Guard":
        """The exclusion as the read paths load it; ``people`` (``proactive_engine._people``:
        the captured directory over the same exclusion) decides addresses — an outside
        domain's address is no person of ours, an inside one is resolved or not clear."""
        try:
            from .pg.permissions import load_exclusion
            ex = load_exclusion()
        except Exception:  # noqa: BLE001 - no guard: nothing is learned
            ex = None
        clear = getattr(people, "identity_clear", None) if people is not None else None
        return cls(ex if ex is not None else _UNAVAILABLE, clear=clear)

    @property
    def available(self) -> bool:
        if self._ex is None:
            return self._clear is not None      # a decider given outright (tests)
        try:
            return not self._ex.unavailable
        except Exception:  # noqa: BLE001
            return False

    def clear(self, identity: Any) -> bool:
        if not isinstance(identity, str) or not identity.strip():
            return False
        if not self.available:
            return False
        if self._clear is not None:
            try:
                return bool(self._clear(identity))
            except Exception:  # noqa: BLE001 - undecidable: not clear
                return False
        try:
            return not self._ex.is_excluded(identity.strip())
        except Exception:  # noqa: BLE001 - undecidable (an unresolved address): not clear
            return False


# ----------------------------------------------------------------------------- signals

@dataclass(frozen=True)
class Signal:
    kind: str
    person: str                    # the person's id (the item owner's, the reader's key)
    at: str                        # ISO UTC
    ref: str                       # where it came from (item id, feedback id, ...)
    data: Mapping[str, Any] = field(default_factory=dict)
    people: tuple[str, ...] = ()   # counterparts (cleared by the exclusion, then dropped)
    texts: tuple[str, ...] = ()    # texts checked for finance (never stored)

    def row(self) -> tuple:
        data = dict(self.data)
        if self.people:     # kept so a counterpart excluded later is purged (fail closed)
            data[_PEOPLE] = sorted(set(self.people))
        return (self.ref, self.kind, self.person, self.at,
                json.dumps(data, sort_keys=True, default=str))

    @classmethod
    def stored(cls, ref: str, kind: str, person: str, at: str, data: str) -> "Signal":
        d = json.loads(data)
        people = d.pop(_PEOPLE, None) or ()
        return cls(kind=kind, person=person, at=at, ref=ref, data=d,
                   people=tuple(str(p) for p in people))

    def cleared(self, guard: "Guard") -> bool:
        """The person and every counterpart are clear under ``guard`` now."""
        return guard.clear(self.person) and all(guard.clear(p) for p in self.people)


_PEOPLE = "_people"


def admit(signals: Iterable[Signal], guard: Guard) -> tuple[list[Signal], Counter]:
    """The signals that may teach anything (module doc, Privacy); raises ``Blocked`` when
    the exclusion cannot decide. Returns (admitted, dropped by reason)."""
    if not guard.available:
        raise Blocked("people_exclusion_unavailable")
    kept, dropped = [], Counter()
    for s in signals:
        if s.kind not in SIGNAL_KINDS:
            dropped["kind_unknown"] += 1
        elif instant(s.at) is None:
            dropped["time_unknown"] += 1
        elif not guard.clear(s.person):
            dropped["person_not_cleared"] += 1
        elif any(not guard.clear(p) for p in s.people):
            dropped["counterpart_not_cleared"] += 1
        elif any(finance_text(t) for t in s.texts) or _finance_data(s.data):
            dropped["finance"] += 1
        elif set(s.data.get("markings") or ()) & {"finance", "pii"} \
                or s.data.get("markings") is None and s.kind in (
                    "answer_rated", "answer_corrected"):
            dropped["finance_marking"] += 1   # an answer served under finance/PII (unknown: so)
        else:
            kept.append(s)
    return kept, dropped


def _finance_data(data: Mapping[str, Any]) -> bool:
    for key in ("added", "dropped", "kept"):
        if any(finance_text(t) for t in data.get(key) or ()):
            return True
    return any(finance_text(data.get(k)) for k in ("value", "question", "signoff_text"))


# ----------------------------------------------------------------------------- lessons

@dataclass
class Lesson:
    lesson_id: str
    scope: str            # "person" | "role"
    who: str
    kind: str
    key: str
    value: str
    confidence: float
    weight: float         # decayed evidence for the value
    total: float          # decayed evidence for every value of the key
    seen: int             # observations of the value
    first_at: str
    last_at: str
    evidence: list[dict[str, str]]
    text: str = ""

    def public(self) -> dict[str, Any]:
        return {"lesson_id": self.lesson_id, "scope": self.scope, "kind": self.kind,
                "key": self.key, "value": self.value, "text": self.text,
                "confidence": round(self.confidence, 3), "seen": self.seen,
                "last_at": self.last_at}


def _weight(at: datetime, now: datetime, kind: str) -> float:
    age = max(0.0, (now - at).total_seconds() / 86400.0)
    return 0.5 ** (age / HALF_LIFE[kind])


class _Votes:
    def __init__(self) -> None:
        self.w: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.n: dict[tuple, Counter] = defaultdict(Counter)
        self.ev: dict[tuple, dict[str, list[tuple[str, str]]]] = defaultdict(
            lambda: defaultdict(list))
        self.texts: dict[tuple, dict[str, str]] = defaultdict(dict)

    def add(self, person: str, kind: str, key: str, value: str, at: datetime, now: datetime,
            ref: str, *, weight: float = 1.0, text: str = "") -> None:
        k = (person, kind, key)
        self.w[k][value] += weight * _weight(at, now, kind)
        self.n[k][value] += 1
        self.ev[k][value].append((_iso(at), ref))
        if text:
            self.texts[k][value] = text


def _text_of(kind: str, key: str, value: str, extra: str = "") -> str:
    if kind == "style":
        if key == "greeting":
            return {"none": "Starts replies without a greeting",
                    "name_only": "Starts replies with just the person's name"}.get(
                value, f"Starts replies with \"{value.replace('_name', ' <name>').replace('_', ' ').capitalize()},\"")
        if key == "signoff":
            return "Ends replies with no sign-off" if value == "none" \
                else f"Ends replies with \"{extra or SIGNOFF_TEXT.get(value, value)}\""
        if key == "length":
            return f"Keeps replies to about {value} words"
        if key.startswith("add:"):
            return f"Usually adds: \"{extra}\""
        if key.startswith("drop:"):
            return f"Takes out: \"{extra}\""
    if kind == "source":
        system = key.split(":", 1)[1]
        return (f"Finds answers from {system} useful" if value == "good"
                else f"Often finds answers from {system} unhelpful")
    if kind == "decision":
        what = key.split(":", 1)[1].replace("|", ", ")
        return (f"Usually acts on prepared items ({what})" if value == "act"
                else f"Usually leaves prepared items ({what})")
    if kind == "detector":
        what = key.split(":", 1)[1].replace("_", " ")
        return (f"Dismisses \"{what}\" alerts" if value == "dismiss"
                else f"Finds \"{what}\" alerts useful")
    if kind == "fact":
        return extra
    return ""


def distil(signals: Iterable[Signal], *, now: datetime,
           roles: Mapping[str, Sequence[str]] | None = None,
           forgotten: Iterable[tuple[str, str, str]] = ()) -> list[Lesson]:
    """Person and role lessons from admitted signals (module doc). ``roles``: role -> its
    members' ids; ``forgotten``: (person, kind, key) never learned again."""
    votes = _Votes()
    lengths: dict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    drafted: dict[str, float] = defaultdict(float)   # decayed weight of replies to a draft
    for s in signals:
        at = instant(s.at)
        if at is None or at > now:
            continue
        d = s.data
        if s.kind in ("draft_sent", "replied"):
            votes.add(s.person, "style", "greeting", str(d.get("greeting") or "none"), at,
                      now, s.ref)
            votes.add(s.person, "style", "signoff", str(d.get("signoff") or "none"), at, now,
                      s.ref, text=str(d.get("signoff_text") or ""))
            if s.kind == "draft_sent":
                drafted[s.person] += _weight(at, now, "style")
            if isinstance(d.get("words"), int) and d["words"] > 0:
                lengths[s.person].append((at, d["words"], s.ref))
            for t in (d.get("added") or ()) if s.kind == "draft_sent" else ():
                votes.add(s.person, "style", "add:" + digest(t), "yes", at, now, s.ref, text=t)
            for t in d.get("dropped") or ():
                votes.add(s.person, "style", "drop:" + digest(t), "yes", at, now, s.ref, text=t)
            for t in d.get("kept") or ():
                votes.add(s.person, "style", "drop:" + digest(t), "no", at, now, s.ref, text=t)
        if s.kind in ("draft_sent", "replied", "item_acted", "draft_ignored"):
            value = "ignore" if s.kind == "draft_ignored" else "act"
            kind_key = "|".join(_slug(d.get(k)) for k in ("autopilot", "channel", "side"))
            votes.add(s.person, "decision", "decision:" + kind_key, value, at, now, s.ref)
            if d.get("sender"):
                votes.add(s.person, "decision", f"sender:{_slug(d.get('autopilot'))}|"
                          f"{d['sender']}", value, at, now, s.ref)
        elif s.kind == "answer_rated":
            value = "good" if (d.get("rating") or 0) > 0 else "bad"
            for system in sorted({_slug(x) for x in d.get("systems") or () if x}):
                votes.add(s.person, "source", "source:" + system, value, at, now, s.ref)
        elif s.kind == "reasked":
            for system in sorted({_slug(x) for x in d.get("systems") or () if x}):
                votes.add(s.person, "source", "source:" + system, "bad", at, now, s.ref,
                          weight=0.5)
        elif s.kind == "answer_corrected":
            question, value = str(d.get("question") or ""), str(d.get("value") or "")
            if question.strip() and value.strip():
                text = f"Asked \"{question.strip()[:160]}\", the right answer is " \
                       f"\"{value.strip()[:300]}\""
                votes.add(s.person, "fact", "fact:" + digest(_norm(question)),
                          value.strip()[:300], at, now, s.ref, text=text)
        elif s.kind == "alert_verdict":
            kind = _slug(d.get("item_kind") or "unknown")
            value = "keep" if d.get("verdict") == "useful" else "dismiss"
            votes.add(s.person, "detector", "detector:" + kind, value, at, now, s.ref)
    for person, rows in lengths.items():
        words = sorted(w for _a, w, _r in rows)
        median = words[len(words) // 2]
        for at, w, ref in rows:
            # a reply within half to double the median agrees; others count against
            votes.add(person, "style", "length", str(median) if median / 2 <= w <= median * 2
                      else "other", at, now, ref)

    gone = {(p, k, key) for p, k, key in forgotten}
    lessons: list[Lesson] = []
    for (person, kind, key), values in votes.w.items():
        if (person, kind, key) in gone:
            continue
        total = sum(values.values())
        value, top = max(values.items(), key=lambda kv: (kv[1], kv[0]))
        if kind == "style" and key.startswith(("add:", "drop:")):
            if value != "yes":
                continue
            if key.startswith("add:"):
                # an added sentence counts against every reply that did not add it
                total = max(total, drafted.get(person, 0.0))
        if key == "length" and value == "other":
            continue
        ev = sorted(votes.ev[(person, kind, key)][value])
        conf = top / (total + (DECISION_PRIOR if kind == "decision" else PRIOR))
        lessons.append(Lesson(
            lesson_id="ls_" + digest("person", person, kind, key),
            scope="person", who=person, kind=kind, key=key, value=value,
            confidence=conf, weight=top, total=total,
            seen=int(votes.n[(person, kind, key)][value]), first_at=ev[0][0],
            last_at=ev[-1][0], evidence=[{"at": a, "ref": r} for a, r in ev[-EVIDENCE_KEPT:]],
            text=_text_of(kind, key, value, votes.texts[(person, kind, key)].get(value, ""))))
    lessons.extend(_role_lessons(lessons, roles or {}))
    lessons.sort(key=lambda l: (l.scope, l.who, l.kind, l.key))
    return lessons


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "unknown").lower()).strip("_") or "unknown"


def _norm(text: str) -> str:
    return " ".join(sorted(set(_WORD.findall(text.lower())) - _STOP))


def _role_lessons(lessons: Sequence[Lesson], roles: Mapping[str, Sequence[str]]
                  ) -> list[Lesson]:
    by_person: dict[str, dict[tuple[str, str], Lesson]] = defaultdict(dict)
    for l in lessons:
        if l.scope == "person" and l.confidence >= APPLY_AT and ROLE_KEYS.match(l.key):
            by_person[l.who][(l.kind, l.key)] = l
    out = []
    for role, members in sorted(roles.items()):
        members = sorted(set(members))
        found: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
        for m in members:
            for k, l in by_person.get(m, {}).items():
                found[k].append(l)
        for (kind, key), ls in sorted(found.items()):
            values = Counter(l.value for l in ls)
            value, agree = values.most_common(1)[0]
            if agree < ROLE_MIN_PEOPLE:
                continue
            same = [l for l in ls if l.value == value]
            conf = sum(l.confidence for l in same) / len(same) * agree / len(ls)
            out.append(Lesson(
                lesson_id="ls_" + digest("role", role, kind, key), scope="role", who=role,
                kind=kind, key=key, value=value, confidence=conf,
                weight=sum(l.weight for l in same), total=sum(l.total for l in ls),
                seen=sum(l.seen for l in same), first_at=min(l.first_at for l in same),
                last_at=max(l.last_at for l in same),
                # a role lesson cites the member lessons, never a person's signals
                evidence=[{"at": l.last_at, "ref": l.lesson_id} for l in same],
                text=same[0].text))
    return out


# ----------------------------------------------------------------------------- ledger

# The ledger's tables: operating/declared_schema/learning.py (DI-09).


def ledger_dir(state_dir: Optional[Path], environ: Mapping[str, str] | None = None
               ) -> Optional[Path]:
    environ = os.environ if environ is None else environ
    if environ.get(DIR_ENV):
        return Path(environ[DIR_ENV])
    return Path(state_dir) / "learning" if state_dir is not None else None


class Ledger:
    """The learning store: admitted signals (features only), the lessons distilled from
    them, the lessons people forgot and each run's counts."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / FILE
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode = WAL")
        from operating import declared_schema
        from operating.declared_schema import learning as declared
        declared_schema.apply(self.conn, declared)
        for side in ("-wal", "-shm"):
            extra = self.path.with_name(self.path.name + side)
            if extra.exists():
                os.chmod(extra, 0o600)

    def close(self) -> None:
        self.conn.close()

    def take(self, signals: Iterable[Signal]) -> int:
        with self.conn:
            # A later act replaces "left" for the same item (a draft_ignored signal
            # taken while the item was overdue must not block the reply or act).
            return self.conn.executemany(
                "INSERT INTO signals VALUES (?, ?, ?, ?, ?) ON CONFLICT(ref) DO UPDATE SET "
                "kind = excluded.kind, person = excluded.person, at = excluded.at, "
                "data = excluded.data WHERE signals.kind = 'draft_ignored' AND "
                "excluded.kind IN ('draft_sent', 'replied', 'item_acted')",
                [s.row() for s in signals]).rowcount

    def signals(self) -> list[Signal]:
        return [Signal.stored(*row) for row in self.conn.execute(
                    "SELECT ref, kind, person, at, data FROM signals ORDER BY at, ref")]

    def forgotten(self) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self.conn.execute("SELECT who, kind, key FROM forgotten")]

    def forget(self, who: str, kind: str, key: str, now: datetime) -> bool:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO forgotten VALUES (?, ?, ?, ?)",
                              (who, kind, key, _iso(now)))
            return self.conn.execute(
                "DELETE FROM lessons WHERE scope = 'person' AND who = ? AND kind = ? AND "
                "key = ?", (who, kind, key)).rowcount > 0

    def write(self, lessons: Sequence[Lesson], summary: Mapping[str, Any], now: datetime
              ) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM lessons")
            self.conn.executemany(
                "INSERT INTO lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(l.lesson_id, l.scope, l.who, l.kind, l.key, l.value, l.text, l.confidence,
                  l.weight, l.total, l.seen, l.first_at, l.last_at, json.dumps(l.evidence))
                 for l in lessons])
            self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)",
                              (_iso(now), json.dumps(dict(summary), sort_keys=True)))

    def lessons(self) -> list[Lesson]:
        return [Lesson(lesson_id=r[0], scope=r[1], who=r[2], kind=r[3], key=r[4], value=r[5],
                       text=r[6], confidence=r[7], weight=r[8], total=r[9], seen=r[10],
                       first_at=r[11], last_at=r[12], evidence=json.loads(r[13]))
                for r in self.conn.execute("SELECT * FROM lessons ORDER BY lesson_id")]


def run(ledger: Ledger, new: Iterable[Signal], *, guard: Guard, now: datetime,
        roles: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Take in new signals (through ``admit``) and distil every lesson again from all kept
    signals. ``Blocked`` (nothing taken in, nothing changed) when the exclusion cannot
    decide. Signals of a person the exclusion now covers are dropped from the ledger."""
    admitted, dropped = admit(new, guard)
    taken = ledger.take(admitted)
    kept = []
    gone = 0
    for s in ledger.signals():
        if s.cleared(guard):
            kept.append(s)
        else:
            gone += 1
            with ledger.conn:
                ledger.conn.execute("DELETE FROM signals WHERE ref = ?", (s.ref,))
    lessons = distil(kept, now=now, roles=roles, forgotten=ledger.forgotten())
    summary = {"schema": SCHEMA, "taken": taken, "dropped": dict(dropped),
               "purged": gone, "signals": len(kept),
               "lessons": dict(Counter(f"{l.scope}:{l.kind}" for l in lessons)),
               "applied": sum(1 for l in lessons if l.confidence >= APPLY_AT)}
    ledger.write(lessons, summary, now)
    return summary


# ----------------------------------------------------------------------------- applying

_CACHE: dict[str, tuple[tuple, "Lessons"]] = {}
_CACHE_LOCK = threading.Lock()


class Lessons:
    """The lessons as the drafter, the alert list and the memory read them."""

    def __init__(self, lessons: Iterable[Lesson], examples: Iterable[Signal] = ()) -> None:
        self.by: dict[tuple[str, str], dict[str, Lesson]] = defaultdict(dict)
        for l in lessons:
            self.by[(l.scope, l.who)][f"{l.kind}:{l.key}"] = l
        self.examples: dict[str, list[Signal]] = defaultdict(list)
        for s in examples:
            if s.kind in ("draft_sent", "replied") and (s.data.get("topic")
                                                        or s.data.get("sender")):
                self.examples[s.person].append(s)

    @classmethod
    def load(cls, directory: Optional[Path], environ: Mapping[str, str] | None = None,
             guard: Optional["Guard"] = None) -> "Lessons":
        """The ledger's lessons as the people exclusion allows them now: none when off,
        absent, unreadable or the exclusion is unavailable (fail closed); a person it now
        covers has no lessons and no examples (``guard``: the default reads the exclusion
        as the read paths do). Read-only; cached by the ledger's and its log's file state."""
        environ = os.environ if environ is None else environ
        if environ.get(OFF_ENV) == "1" or directory is None:
            return cls(())
        guard = Guard.from_env() if guard is None else guard
        if not guard.available:
            return cls(())
        path = Path(directory) / FILE
        try:
            st = os.stat(path)
        except OSError:
            return cls(())
        try:
            wal = os.stat(str(path) + "-wal")
            wal_stamp = (wal.st_mtime_ns, wal.st_size)
        except OSError:
            wal_stamp = None
        stamp = (st.st_mtime_ns, st.st_size, wal_stamp)
        with _CACHE_LOCK:
            hit = _CACHE.get(str(path))
        if hit and hit[0] == stamp:
            raw = hit[1]
        else:
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
                try:
                    lessons = [Lesson(lesson_id=r[0], scope=r[1], who=r[2], kind=r[3],
                                      key=r[4], value=r[5], text=r[6], confidence=r[7],
                                      weight=r[8], total=r[9], seen=r[10], first_at=r[11],
                                      last_at=r[12], evidence=json.loads(r[13]))
                               for r in conn.execute("SELECT * FROM lessons ORDER BY lesson_id")]
                    signals = [Signal.stored(*row) for row in conn.execute(
                                   "SELECT ref, kind, person, at, data FROM signals WHERE "
                                   "kind IN ('draft_sent', 'replied') ORDER BY at, ref")]
                finally:
                    conn.close()
            except (sqlite3.Error, OSError, ValueError):
                return cls(())
            raw = (lessons, signals)
            with _CACHE_LOCK:
                _CACHE[str(path)] = (stamp, raw)
        people = {l.who for l in raw[0] if l.scope == "person"} | {
            p for s in raw[1] for p in (s.person, *s.people)}
        cleared = {p for p in people if guard.clear(p)}
        return cls([l for l in raw[0] if l.scope == "role" or l.who in cleared],
                   [s for s in raw[1] if s.person in cleared
                    and all(p in cleared for p in s.people)])

    def __bool__(self) -> bool:
        return bool(self.by)

    def get(self, person: str, roles: Iterable[str], kind: str, key: str,
            at: float = APPLY_AT) -> Optional[Lesson]:
        """The person's own lesson for ``kind:key`` at ``at`` confidence or more; else (only
        when the person has none of their own) a role's."""
        own = self.by.get(("person", person), {}).get(f"{kind}:{key}")
        if own is not None:
            return own if own.confidence >= at else None
        best = None
        for role in sorted(set(roles)):
            got = self.by.get(("role", role), {}).get(f"{kind}:{key}")
            if got is not None and got.confidence >= at and (
                    best is None or got.confidence > best.confidence):
                best = got
        return best

    def of(self, person: str) -> list[Lesson]:
        return sorted(self.by.get(("person", person), {}).values(),
                      key=lambda l: (l.kind, l.key))

    # -- drafts --

    def apply_draft(self, work: Mapping[str, Any], person: str, roles: Iterable[str] = (),
                    *, event: Mapping[str, Any] | None = None,
                    autopilot: Optional[str] = None, before: Optional[str] = None
                    ) -> dict[str, Any]:
        """``work`` with its email or Teams draft re-framed by the person's lessons, then
        shaped by their own past replies most like this message (``learning_fewshot``:
        greeting, sign-off, the opening and closing sentences they write again and again,
        generic sentences they always take out), and a ``learned`` note (the lessons used,
        the few-shot parts applied, the examples); the same ``work`` when nothing applies.
        Never changes a sentence with a figure. ``before``: examples only from before it."""
        from . import learning_fewshot as fs
        roles = tuple(roles)
        out = dict(work)
        used: list[Lesson] = []
        shaped: list[str] = []
        nb = fs.neighbours(self.examples.get(person, ()), event or {}, autopilot,
                           before=before) if event is not None else []
        for slot in ("draft", "teams_draft"):
            draft = work.get(slot)
            if not isinstance(draft, Mapping) or not isinstance(draft.get("body"), str):
                continue
            body, lessons = self.restyle(draft["body"], person, roles,
                                         chat=slot == "teams_draft")
            parts: list[str] = []
            if slot == "draft" and nb:
                body, parts = fs.compose(body, nb, autopilot)
            if lessons or parts:
                out[slot] = {**draft, "body": body}
                used.extend(l for l in lessons if l not in used)
                shaped.extend(p for p in parts if p not in shaped)
        examples = [{"item": ex.data.get("item"), "at": ex.at, "match": round(sc, 2)}
                    for sc, ex in nb[:EXAMPLES]] or self.similar(person, event or {})
        if not used and not examples and not shaped:
            return dict(work)
        out["learned"] = {"lessons": [{k: v for k, v in l.public().items() if k != "text"}
                                      for l in used],
                          "few_shot": shaped, "examples": examples}
        return out

    def drafter_prompt(self, person: str, event: Mapping[str, Any], autopilot: Optional[str],
                       text_of: Callable[[Signal], Any], roles: Iterable[str] = ()) -> str:
        """The few-shot block for a model drafter (``learning_fewshot.prompt_block``): the
        person's style rules and their own past (draft, sent) pairs most like ``event``;
        ``text_of(signal)`` reads a pair's (draft, reply) words from the store."""
        from . import learning_fewshot as fs
        nb = fs.neighbours(self.examples.get(person, ()), event, autopilot)
        return fs.prompt_block(nb, fs.rules(self.of(person)), text_of)

    def restyle(self, body: str, person: str, roles: Sequence[str], *, chat: bool = False
                ) -> tuple[str, list[Lesson]]:
        from . import copy_gate as gate
        if chat:
            # the lessons are learned from mail replies: a chat reply keeps its own form
            return body, []
        lines = body.split("\n")
        used: list[Lesson] = []
        idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        greet = self.get(person, roles, "style", "greeting")
        if greet is not None and idx is not None and not chat:
            form, name = greeting_of(lines[idx])
            if form != "none" and form != greet.value:
                new = _greeting_line(greet.value, name)
                if new is None or (greet.value.endswith("_name") or greet.value == "name_only") \
                        and not name:
                    pass   # the lesson needs a name the draft does not have
                elif new == "":
                    del lines[idx]
                    while idx < len(lines) and not lines[idx].strip():
                        del lines[idx]
                    used.append(greet)
                else:
                    lines[idx] = new
                    used.append(greet)
        drop = [l for l in self.by.get(("person", person), {}).values()
                if l.kind == "style" and l.key.startswith("drop:") and l.confidence >= APPLY_AT]
        if drop:
            drops = {_template_from_text(l.text): l for l in drop}
            drops.pop(None, None)

            def content(rows: Sequence[str]) -> int:
                return sum(1 for ln in rows if ln.strip() and greeting_of(ln)[0] == "none"
                           and not _CLOSE.match(ln.strip())
                           for _s in _SENTENCE.findall(ln) if _s.strip())

            trimmed, hit = [], []
            for ln in lines:
                if _FIGURE.search(ln) or _ADDRESS.search(ln):
                    trimmed.append(ln)   # a line with a figure or address is never touched
                    continue
                sentences = _SENTENCE.findall(ln)
                keep = [x for x in sentences if template(x) not in drops]
                hit += [drops[template(x)] for x in sentences if template(x) in drops]
                trimmed.append(" ".join(x.strip() for x in keep) if len(keep) < len(sentences)
                               else ln)
            # never a draft left with nothing to say: then nothing is dropped
            if hit and content(trimmed) >= 1:
                lines = [ln for i, ln in enumerate(trimmed)
                         if ln.strip() or (i and trimmed[i - 1].strip())]
                for lesson in hit:
                    if lesson not in used:
                        used.append(lesson)
        sign = self.get(person, roles, "style", "signoff")
        tail = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
        has_close = tail is not None and bool(_CLOSE.match(lines[tail].strip()))
        adds = [l for l in self.by.get(("person", person), {}).values()
                if l.kind == "style" and l.key.startswith("add:") and l.confidence >= APPLY_AT]
        for lesson in sorted(adds, key=lambda l: -l.confidence)[:1]:
            sentence = _sentence_from_text(lesson.text)
            if not sentence or sentence.lower() in body.lower():
                continue
            if not gate.check(sentence, surface="draft_body")["ok"]:
                continue
            at = tail if has_close else (tail + 1 if tail is not None else 0)
            lines[at:at] = ([sentence, ""] if has_close else ["", sentence])
            tail = at + 2 if has_close else at + 1
            used.append(lesson)
        if sign is not None:
            tail = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
            has_close = tail is not None and bool(_CLOSE.match(lines[tail].strip()))
            current = signoff_of(lines[tail])[0] if has_close else "none"
            if current != sign.value:
                literal = sign.text.split("\"")[1] if "\"" in sign.text else \
                    SIGNOFF_TEXT.get(sign.value)
                if sign.value == "none":
                    del lines[tail]
                elif has_close:
                    lines[tail] = literal or lines[tail]
                elif tail is not None and literal:
                    lines[tail + 1:] = ["", literal]
                used.append(sign)
        out = "\n".join(lines).strip("\n")
        return (out, used) if used else (body, [])

    def similar(self, person: str, event: Mapping[str, Any], most: int = EXAMPLES
                ) -> list[dict[str, Any]]:
        """The person's own past draft-to-reply pairs most like this message (references
        and the share of topic words shared; the words are read from the capture)."""
        keys = set(topic_keys(" ".join(str(event.get(k) or "") for k in ("subject", "text"))))
        if not keys:
            return []
        scored = []
        for s in self.examples.get(person, ()):
            theirs = set(s.data.get("topic") or ())
            share = len(keys & theirs) / len(keys | theirs) if theirs else 0.0
            if share >= 0.2:
                scored.append((share, s.at, s))
        scored.sort(key=lambda x: (-x[0], x[1]))
        # the past item only: its own citations decide who may open it, and its reply is
        # found from its thread
        return [{"item": s.data.get("item"), "at": s.at, "match": round(share, 2)}
                for share, _a, s in scored[:most]]

    # -- items --

    def skip_reason(self, person: str, roles: Iterable[str], autopilot: str,
                    event: Mapping[str, Any]) -> Optional[str]:
        """Why an item is not prepared for ``person``: they leave items of this kind (or
        from this sender) — confidence ``SKIP_AT`` on ``SKIP_MIN`` evidence; else None."""
        roles = tuple(roles)
        sender = sender_key(event.get("sender"))
        keys = []
        if sender:
            keys.append(f"sender:{_slug(autopilot)}|{sender}")
        keys.append("decision:" + "|".join(_slug(x) for x in (
            autopilot, event.get("channel"), event.get("side"))))
        for key in keys:
            # the person's own behaviour only: never left out because colleagues leave it
            own = self.by.get(("person", person), {}).get(f"decision:{key}")
            if own is None:
                continue
            if own.value == "ignore" and own.confidence >= SKIP_AT and own.weight >= SKIP_MIN:
                return f"learned_left:{own.lesson_id}"
            return None    # the narrower lesson decides (the person acts on these)
        return None

    # -- alerts --

    def alert_weight(self, reader: str, kind: str, roles: Iterable[str] = ()
                     ) -> tuple[float, Optional[str]]:
        """(rank weight in (0, 1], the lesson) for an alert of ``kind`` to ``reader``: one
        for a kind they keep or never judged; down to 0.3 as they dismiss it."""
        l = self.get(person_id(reader), roles, "detector", "detector:" + _slug(kind))
        if l is None or l.value != "dismiss" or l.confidence < APPLY_AT:
            return 1.0, None
        return round(max(0.3, 1.0 - 0.7 * l.confidence), 3), l.lesson_id

    # -- memory --

    def memory_items(self, person: str) -> list[dict[str, Any]]:
        """The person's lessons at ``APPLY_AT`` or more as ``operator_api.memory`` items
        (``remember`` keywords): style, source, decision and detector are preferences, a
        corrected answer a fact; the subject keys supersession."""
        out = []
        for l in self.of(person):
            if l.confidence < APPLY_AT or not l.text or l.key.startswith("sender:"):
                continue
            out.append({"kind": "fact" if l.kind == "fact" else "preference",
                        "subject": f"learn.{l.kind}.{l.key}"[:300], "text": l.text,
                        "source": "learn", "source_ref": f"{l.lesson_id}@{l.last_at}",
                        "confidence": round(min(1.0, max(0.05, l.confidence)), 3),
                        "at": instant(l.last_at)})
        return out


def sender_key(address: Any) -> Optional[str]:
    text = str(address or "").strip().lower()
    return digest("party", text) if "@" in text else None


def _greeting_line(form: str, name: Optional[str]) -> Optional[str]:
    if form == "none":
        return ""
    base = {"hi_name": "Hi {n},", "hello_name": "Hello {n},", "hey_name": "Hey {n},",
            "dear_name": "Dear {n},", "good_morning_name": "Good morning {n},",
            "name_only": "{n},", "hi": "Hi,", "hello": "Hello,",
            "good_morning": "Good morning,"}.get(form)
    if base is None:
        return None
    if "{n}" in base:
        return base.format(n=name) if name else None
    return base


def _sentence_from_text(text: str) -> Optional[str]:
    found = re.search(r"\"(.+)\"", text or "")
    if not found:
        return None
    sentence = re.sub(r"\bi\b", "I", found.group(1).strip())
    return sentence[:1].upper() + sentence[1:]


def _template_from_text(text: str) -> Optional[str]:
    found = re.search(r"\"(.+)\"", text or "")
    return found.group(1) if found else None


def publish(lessons: Lessons, memory: Any, reader_for: Callable[[str], Any],
            people: Iterable[str]) -> Counter:
    """Each person's applied lessons into their memory (``memory.remember``); a person
    ``reader_for`` gives no reader for is skipped. Returns counts."""
    done: Counter = Counter()
    for person in sorted(set(people)):
        reader = reader_for(person)
        if reader is None:
            done["no_reader"] += 1
            continue
        for item in lessons.memory_items(person):
            try:
                got = memory.remember(reader, **item)
            except Exception:  # noqa: BLE001 - memory never stops learning
                done["failed"] += 1
                continue
            done["kept" if got else "refused"] += 1
    return done


# ----------------------------------------------------------------------------- CLI

def _roles_of(state_dir: Optional[Path]) -> dict[str, list[str]]:
    for path in (os.environ.get("PLX_AUTOPILOT_ROLES_FILE"),
                 state_dir and Path(state_dir) / "autopilot" / "autopilot-roles.json"):
        if path and Path(path).is_file():
            try:
                raw = json.loads(Path(path).read_text("utf-8"))
                roles = raw.get("roles", raw) if isinstance(raw, dict) else {}
                return {str(r): [str(m) for m in ms] for r, ms in roles.items()
                        if isinstance(ms, list)}
            except (OSError, ValueError):
                return {}
    return {}


_SYNCED: dict[str, tuple] = {}
_SYNCED_LOCK = threading.Lock()


def sync_memory(memory: Any, reader: Any, state_dir: Optional[Path],
                environ: Mapping[str, str] | None = None) -> Optional[Counter]:
    """Hand this reader's own applied lessons to their memory (``memory.remember`` with the
    live reader, so each lands under their own key), once per ledger change per reader;
    what the Ask's memory then puts in front of the agent. Never raises (memory and
    learning never fail an Ask); None when nothing was due."""
    try:
        directory = ledger_dir(state_dir, environ)
        lessons = Lessons.load(directory, environ)
        if not lessons or memory is None:
            return None
        key = str(getattr(reader, "key", "") or "")
        path = Path(directory) / FILE
        found = os.stat(path)
        try:
            wal = os.stat(str(path) + "-wal")
            wal_stamp = (wal.st_mtime_ns, wal.st_size)
        except OSError:
            wal_stamp = None
        stamp = (found.st_mtime_ns, found.st_size, wal_stamp)
        with _SYNCED_LOCK:
            if _SYNCED.get(key) == stamp:
                return None
            _SYNCED[key] = stamp
        person = person_id(key)
        done = publish(lessons, memory, lambda who: reader if who == person else None,
                       [person])
        if done.get("failed"):
            with _SYNCED_LOCK:
                _SYNCED.pop(key, None)    # tried again on the next Ask
        return done
    except Exception:  # noqa: BLE001 - learning never fails what the person asked
        return None


def ask_rule(reader_key: str, state_dir: Optional[Path],
             environ: Mapping[str, str] | None = None, most: int = 8) -> str:
    """The reader's own applied lessons for the Ask's task (how they want answers and
    which facts they corrected), as plain lines; empty when none. Never raises."""
    try:
        lessons = Lessons.load(ledger_dir(state_dir, environ), environ)
        rows = [l for l in lessons.of(person_id(reader_key))
                if l.confidence >= APPLY_AT and l.kind in ("fact", "source") and l.text]
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    rows.sort(key=lambda l: -l.confidence)
    return ("What this reader taught earlier (their own corrections and ratings; a fact "
            "here is what they said, still stated only through a claim a source supports):\n"
            + "\n".join(f"- {l.text}" for l in rows[:most]))


# The memory store that serves an offline writer by person id (``remember_for``), set by
# the layer that owns it; operating never imports operator_api (tools/layer_contract.py),
# so the CLI hands lessons over only when a store was registered here.
MEMORY_FACTORY: Optional[Callable[[], Any]] = None


def _publish_to_memory(lessons: Lessons, factory: Optional[Callable[[], Any]] = None) -> Any:
    """Each person's applied lessons into their memory, when a registered memory serves
    an offline writer by person id (``remember_for``); else what it waits on."""
    factory = factory or MEMORY_FACTORY
    if factory is None:
        return "memory_not_available"
    try:
        memory = factory()
    except Exception:  # noqa: BLE001
        memory = None
    if memory is None:
        return "memory_store_unreachable"
    if getattr(memory, "remember_for", None) is None:
        return "memory_has_no_offline_writer"

    class _ById:
        def remember(self, person: str, **item: Any) -> Any:
            return memory.remember_for(person, **item)

    people = {who for (scope, who) in lessons.by if scope == "person"}
    return dict(publish(lessons, _ById(), lambda person: person, people))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.learning")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("distil")
    d.add_argument("--state-dir", required=True, type=Path)
    d.add_argument("--store", default=None)
    d.add_argument("--source", default=None)
    s = sub.add_parser("show")
    s.add_argument("--state-dir", required=True, type=Path)
    s.add_argument("--person", required=True)
    f = sub.add_parser("forget")
    f.add_argument("--state-dir", required=True, type=Path)
    f.add_argument("--person", required=True)
    f.add_argument("--kind", required=True, choices=LESSON_KINDS)
    f.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    directory = ledger_dir(args.state_dir)
    now = datetime.now(timezone.utc)
    ledger = Ledger(directory)
    try:
        if args.cmd == "forget":
            print(json.dumps({"forgotten": ledger.forget(args.person, args.kind, args.key,
                                                         now)}))
            return 0
        if args.cmd == "show":
            print(json.dumps([l.public() for l in Lessons(ledger.lessons()).of(args.person)],
                             indent=1))
            return 0
        from . import learning_signals as ls
        try:
            new, sources = ls.collect(args.state_dir, store=args.store, source=args.source,
                                      now=now)
            people = None
            if args.store:
                from . import proactive_engine as pe
                people = pe._people(Path(args.store))
            summary = run(ledger, new, guard=Guard.from_env(people), now=now,
                          roles=_roles_of(args.state_dir))
        except Blocked as exc:
            print(json.dumps({"status": "blocked", "reason": str(exc)}))
            return 3
        summary["sources"] = sources
        summary["memory"] = _publish_to_memory(Lessons(ledger.lessons()))
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
