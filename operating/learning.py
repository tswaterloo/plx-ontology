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
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from operating.file_modes import keep_shared
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
REPEAT_AT = 3     # the same reply shape this many times: offered as a template
SHAPE_ENDS = 2       # a reply's first and last sentences kept as templates (few-shot)
# role lessons: categorical keys only (never a fact, a phrase or a sender)
ROLE_KEYS = re.compile(r"^(?:greeting|signoff|length|source:[a-z_]+|detector:[a-z_]+|"
                       r"decision:[a-z_]+\|[a-z_]+\|[a-z_]+)$")

# the sides of a correspondent a person's greeting and sign-off are learned for apart (a
# person writes "Hi Bob," to a customer and "Bob -" to a colleague), and the frame keys
# learned per side (``side:<side>:<key>``) and per correspondent (``sender:<digest>:<key>``)
AUDIENCES = {"customer": "customers", "supplier": "suppliers", "internal": "colleagues"}
FRAME_KEYS = ("greeting", "signoff")

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


def describe_edit(draft: str, sent: str) -> list[str]:
    """What a person's edit of a draft changed, in the words a lesson is shown in
    (``_text_of``: greeting, sign-off, length, a generic sentence added or taken out; never
    a sentence with a figure, an address or a name): what the Workbench learns from it
    (``draft_sent``, distilled with every other edit of theirs). [] when nothing of that
    kind changed, or either text is empty or touches pay or finance."""
    if not str(draft or "").strip() or not str(sent or "").strip() \
            or finance_text(draft) or finance_text(sent):
        return []
    feats = edit_features(draft, sent)
    out: list[str] = []
    was_greet, _n = greeting_of(reply_core(draft))
    if feats["greeting"] != was_greet:
        out.append(_text_of("style", "greeting", feats["greeting"]))
    was_close, _l = signoff_of(reply_core(draft))
    if feats["signoff"] != was_close:
        out.append(_text_of("style", "signoff", feats["signoff"],
                            feats.get("signoff_text") or ""))
    before = len(" ".join(body_sentences(draft)).split())
    if before and feats["words"] and abs(feats["words"] - before) >= max(8, before * 0.3):
        out.append(_text_of("style", "length", str(int(round(feats["words"], -1)) or
                                                   feats["words"])))
    out += [_text_of("style", "add:x", "", t) for t in feats["added"][:2]]
    out += [_text_of("style", "drop:x", "", t) for t in feats["dropped"][:2]]
    return [x.replace("Usually adds:", "Adds:", 1) for x in out if x][:5]


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
    if kind == "style" and key.startswith(("side:", "sender:")):
        scope, base = key.split(":", 1)[0], key.rsplit(":", 1)[1]
        said = _text_of(kind, base, value, extra)
        if scope == "sender":
            return said.replace("replies", "replies to this person", 1)
        who = AUDIENCES.get(key.split(":")[1], "")
        return said.replace("replies", f"replies to {who}", 1) if who else said
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
            side = _slug(d.get("side"))
            for key in FRAME_KEYS:
                value = str(d.get(key) or "none")
                text = str(d.get("signoff_text") or "") if key == "signoff" else ""
                if side in AUDIENCES:
                    votes.add(s.person, "style", f"side:{side}:{key}", value, at, now, s.ref,
                              text=text)
                if d.get("sender"):
                    votes.add(s.person, "style", f"sender:{d['sender']}:{key}", value, at,
                              now, s.ref, text=text)
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
        if (person, kind, key) in gone or kind == "style" and key.startswith(
                ("side:", "sender:")) and (person, kind, key.rsplit(":", 1)[1]) in gone:
            continue   # forgetting a greeting or sign-off forgets it for every audience
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
        keep_shared(self.directory, 0o700)   # a shared-group directory keeps its group + setgid
        self.path = self.directory / FILE
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        keep_shared(self.path, 0o600)
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode = WAL")
        from operating import declared_schema
        from operating.declared_schema import learning as declared
        declared_schema.apply(self.conn, declared)
        for side in ("-wal", "-shm"):
            extra = self.path.with_name(self.path.name + side)
            if extra.exists():
                keep_shared(extra, 0o600)

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
            done = self.conn.execute(
                "DELETE FROM lessons WHERE scope = 'person' AND who = ? AND kind = ? AND "
                "key = ?", (who, kind, key)).rowcount > 0
            if kind == "style" and key in FRAME_KEYS:
                # a greeting or sign-off forgotten for everyone: for every audience too
                self.conn.execute(
                    "DELETE FROM lessons WHERE scope = 'person' AND who = ? AND kind = ? "
                    "AND (key LIKE 'side:%:' || ? OR key LIKE 'sender:%:' || ?)",
                    (who, kind, key, key))
            return done

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


# ------------------------------------------------------------------- rules a person stated

STATED_FILE = "stated.json"
_STATED_LOCK = threading.Lock()
_MAILS = r"(?:(?:all\s+|every\s+|each\s+)?(?:of\s+)?(?:my\s+|the\s+)?(?:e-?mails?|replies|messages|drafts|mails?)\s+)?"
_STATED_SIGNOFF = re.compile(
    r"\b(?:sign(?:ing)?[\s-]*off|end|close|finish)\s+" + _MAILS +
    r"with\s+[\"“']?(?P<words>[^\"”\n]{2,60}?)[\"”']?\s*[.!]?\s*$", re.I)
_STATED_GREET = re.compile(
    r"\b(?:start|open|begin|greet)\s+" + _MAILS +
    r"with\s+[\"“']?(?P<word>hi|hello|hey|dear|good\s+morning)\b", re.I)
_STATED_NO_GREET = re.compile(r"\b(?:no greeting|without (?:a |any )?greeting|skip the "
                              r"greeting|don'?t (?:start|open) with (?:a )?greeting)\b", re.I)
_STATED_DROP = re.compile(
    r"\b(?:never|don'?t|do not|stop)\s+(?:ever\s+)?(?:say|write|use|open with|end with|put|"
    r"add|saying|writing|using)\s+[\"“](?P<words>[^\"”]{4,160})[\"”]", re.I)
# "to customers sign off with ...": a greeting or sign-off stated for one side only
_STATED_SIDE = re.compile(
    r"\b(?:when\s+(?:writing|replying|emailing)\s+)?(?:to|for)\s+(?:my\s+|our\s+|the\s+)?"
    r"(?P<side>customers?|clients?|suppliers?|vendors?|colleagues?|co-?workers?|team|"
    r"internal\s+(?:mail|e-?mails?|people)|staff)\b,?", re.I)
_SIDE_WORD = {"customer": "customer", "client": "customer", "supplier": "supplier",
              "vendor": "supplier", "colleague": "internal", "coworker": "internal",
              "co-worker": "internal", "team": "internal", "internal": "internal",
              "staff": "internal"}
_GREET_FORM = {"hi": "hi_name", "hello": "hello_name", "hey": "hey_name", "dear": "dear_name",
               "good morning": "good_morning_name"}

# Autopilot rules a person states: which messages get no draft ("stop drafting replies to
# hello@x.com", "don't draft anything for suppliers", "ignore mail from x.com"), who their
# email drafts copy ("always cc ana@petra.com on supplier emails") and who they never copy
# ("never cc bob@x.com"). A target is an address, a domain or a side (customers, suppliers,
# colleagues); a name alone is none (the Ask keeps the rule with the address it resolved).
# Kept as ``decision`` lessons the autopilot obeys (``Lessons.stated_skip``,
# ``Lessons.recipients``, ``Lessons.stated_cc``).
_RULE_ADDRESS = r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+"
_RULE_TARGET = re.compile(r"(?<![\w.@'%+-])(?:" + _RULE_ADDRESS
                          + r"|@?(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24})(?![\w@-])")
_STATED_NO_DRAFT = re.compile(
    r"\b(?:(?:stop|don'?t|do not|never|no(?:\s+more)?|quit|skip)(?:\s+ever)?\s+"
    r"(?:(?:auto-?)?(?:draft|prepar|writ|suggest|mak)\w*\s+)?(?:(?:any|a|the)\s+)?"
    r"(?:(?:e-?mail|mail|teams|chat)\s+)?(?:repl(?:y|ies|ying)|drafts?|answers?|responses?"
    r"|items?|anything)"
    # "ignore"/"mute" only of messages as a whole ("ignore mail from x.com", "mute
    # x@y.com"), never of something in them ("ignore typos from suppliers")
    r"|(?:ignore|mute)(?:\s+(?:all|any|every))?(?:\s+(?:e-?mails?|mail|messages?|"
    r"everything|anything))?(?=\s+(?:from\s+|by\s+)?(?:@|[\w.%+'-]+@|"
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b)|\s+(?:e-?mails?|mail|messages?|everything|"
    r"anything)\b))\b", re.I)
_STATED_COPY = re.compile(
    r"\b(?:(?P<neg>never|don'?t|do not|stop|no longer|quit)(?:\s+ever)?\s+)?"
    r"(?:(?:always|also)\s+)?(?:cc(?:'?d|'?ing|s)?|bcc|cop(?:y|ying|ies)|loop(?:ing)?\s+in"
    r"|includ(?:e|ing)|add(?:ing)?)\s+(?:in\s+)?(?P<who>" + _RULE_ADDRESS
    + r"(?:\s*(?:,|and|&|or)\s*" + _RULE_ADDRESS + r")*)", re.I)
_RULE_SIDE = re.compile(r"\b(customers?|clients?|suppliers?|vendors?|colleagues?|co-?workers?"
                        r"|internal|team|staff)\b", re.I)
# a rule for some messages only ("... about pricing", "unless it is urgent"): no rule the
# autopilot can apply by sender or side; the drafter reads the person's words instead
_RULE_ONLY_SOME = re.compile(
    r"\b(?:about|regarding|concerning|re|unless|except|when|if|while|during|until|before|"
    r"after|weekends?|evenings?|nights?|holidays?|vacation|today|tonight|tomorrow|this week)\b",
    re.I)
RULE_PREFIXES = ("skip:", "cc:", "never:")


def _rule_side(text: str) -> str:
    found = _RULE_SIDE.search(text)
    if not found:
        return "all"
    word = found.group(1).lower()
    return _SIDE_WORD.get(word.rstrip("s")) or _SIDE_WORD.get(word) or "all"


def _rule_target(found: str) -> str:
    target = found.lower().rstrip(".")
    return target if "@" in target[1:] else target.lstrip("@")


def autopilot_rules(text: Any) -> list[dict[str, str]]:
    """The Autopilot rules one sentence states (``_STATED_NO_DRAFT``, ``_STATED_COPY``):
    ``skip:<side>:<target>`` (no draft for a message from the target, ``*`` every one of
    that side), ``cc:<side>:<address>`` (copy them on that side's email drafts, ``all``:
    every one) and ``never:<address>`` (never copied); [] for a sentence stating none,
    naming no address, domain or side, or narrowing it to some messages (``_RULE_ONLY_SOME``)."""
    text = " ".join(str(text or "").split())
    out: dict[str, dict[str, str]] = {}
    for found in _STATED_COPY.finditer(text):
        rest = text[found.end():]
        if _RULE_ONLY_SOME.search(rest):
            continue
        side = _rule_side(rest[:120])
        for address in re.findall(_RULE_ADDRESS, found.group("who")):
            address = address.lower()
            if found.group("neg"):
                out[f"never:{address}"] = {"key": f"never:{address}", "value": "never",
                                           "text": f"Autopilot never copies {address}"}
            else:
                key = f"cc:{side}:{address}"
                out[key] = {"key": key, "value": "cc", "text": (
                    f"Autopilot copies {address} on every email draft" if side == "all" else
                    f"Autopilot copies {address} on email drafts to {AUDIENCES[side]}")}
    said = _STATED_NO_DRAFT.search(text)
    if said and not any(r["value"] == "never" for r in out.values()) \
            and not _RULE_ONLY_SOME.search(text[said.end():]):
        rest = text[said.end():]
        targets = [_rule_target(t) for t in _RULE_TARGET.findall(rest)]
        side = _rule_side(rest)
        for target in targets or (["*"] if side != "all" else []):
            key = f"skip:{side}:{target}"
            out[key] = {"key": key, "value": "skip", "text": (
                f"Autopilot drafts nothing for {AUDIENCES[side]}" if target == "*" else
                f"Autopilot drafts nothing for messages from {target}"
                + (f" ({AUDIENCES[side]})" if side != "all" else ""))}
    return list(out.values())


def rule_matches(target: str, address: Any) -> bool:
    """Whether a stated rule's target (an address, or a domain and its subdomains) is the
    message's ``address``."""
    address = str(address or "").strip().lower()
    if "@" not in address or not target:
        return False
    if "@" in target:
        return address == target
    domain = address.rsplit("@", 1)[1]
    return domain == target or domain.endswith("." + target)


def stated_rules(text: Any) -> list[dict[str, str]]:
    """The drafting rules a person stated in their own words ("sign off with Thanks, Ana",
    "start my emails with Hi <name>", "no greeting", "never write \"hope this finds you
    well\""), as style lessons ({key, value, text}) the drafter applies over anything
    learned, and the Autopilot rules a sentence states (``autopilot_rules``: no draft for a
    sender or side, who is copied or never copied) instead of a style rule. Nothing for pay
    or finance, a sign-off that is no closing words, or a sentence that names someone or
    carries a figure (``template``)."""
    text = " ".join(str(text or "").split())
    if not text or finance_text(text):
        return []
    sentences = [x for x in re.split(r"(?<=[.!?\u201d\"])\s+(?=[A-Z])", text) if x]
    if len(sentences) > 1:   # one rule per sentence; a later one wins for the same key
        merged: dict[str, dict[str, str]] = {}
        for sentence in sentences:
            for rule in stated_rules(sentence):
                merged[rule["key"]] = rule
        return list(merged.values())
    ruled = autopilot_rules(text)
    if ruled:
        return ruled
    side_said = _STATED_SIDE.search(text)
    if side_said and side_said.start() > 0 and text[side_said.end():].strip(" .!"):
        side_said = None   # mid-sentence ("..., and write to the team ..."): not a side
    if side_said:
        word = side_said.group("side").lower().split()[0]
        side = _SIDE_WORD.get(word.rstrip("s")) or _SIDE_WORD.get(word)
        rest = " ".join((text[:side_said.start()] + " " + text[side_said.end():]).split())
        rest = rest.strip(" ,")
        return [({**r, "key": f"side:{side}:{r['key']}",
                  "text": _text_of("style", f"side:{side}:{r['key']}", r["value"],
                                   _quoted(r["text"]))}
                 if side and r["key"] in FRAME_KEYS else r)
                for r in stated_rules(rest[:1].upper() + rest[1:])]
    out: list[dict[str, str]] = []
    found = _STATED_SIGNOFF.search(text)
    if found:
        words = found.group("words").strip().rstrip(".!").strip()
        close = _CLOSE.match(words.split(",")[0])
        if close and len(words) <= 60 and not re.search(r"[\d@]", words):   # no phone/mail
            form = signoff_of(words.split(",")[0])[0]
            out.append({"key": "signoff", "value": form,
                        "text": _text_of("style", "signoff", form, words)})
    if _STATED_NO_GREET.search(text):
        out.append({"key": "greeting", "value": "none",
                    "text": _text_of("style", "greeting", "none")})
    else:
        found = _STATED_GREET.search(text)
        if found:
            form = _GREET_FORM[re.sub(r"\s+", " ", found.group("word").lower())]
            out.append({"key": "greeting", "value": form,
                        "text": _text_of("style", "greeting", form)})
    for found in _STATED_DROP.finditer(text):
        generic = template(found.group("words"))
        if generic:
            out.append({"key": "drop:" + digest(generic), "value": "yes",
                        "text": _text_of("style", "drop:", "yes", generic)})
    return out


def _quoted(text: str) -> str:
    return text.split("\"")[1] if text.count("\"") >= 2 else ""


def _stated_path(directory: Optional[Path]) -> Optional[Path]:
    return Path(directory) / STATED_FILE if directory is not None else None


def _stated_read(path: Path) -> dict[str, dict[str, list[dict[str, str]]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def set_stated(directory: Optional[Path], person: str, item: Any, text: Optional[str]
               ) -> list[dict[str, str]]:
    """Keep (``text``) or drop (None) the rules one memory item of the person's states;
    the rules kept. The file is the person's stated rules by item (0600 in a 0700 folder),
    read by ``Lessons.load``; nothing else of the item is kept."""
    path = _stated_path(directory)
    if path is None or not person:
        return []
    rules = stated_rules(text) if text is not None else []
    with _STATED_LOCK:
        held = _stated_read(path)
        mine = dict(held.get(person) or {})
        if rules:
            mine[str(item)] = rules
        elif str(item) not in mine:
            return []
        else:
            mine.pop(str(item), None)
        if mine:
            held[person] = mine
        else:
            held.pop(person, None)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        keep_shared(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(held, fh, sort_keys=True)
        os.replace(tmp, path)
    return rules


def _stated_lessons(directory: Optional[Path]) -> list[Lesson]:
    """Every person's stated rules as lessons of confidence 1 (the newest item's rule per
    key wins)."""
    path = _stated_path(directory)
    if path is None:
        return []
    out: dict[tuple[str, str], Lesson] = {}
    for person, items in sorted(_stated_read(path).items()):
        if not isinstance(items, dict):
            continue
        for item in sorted(items, key=lambda k: (len(k), k)):   # ids ascend: newest last
            for rule in items[item] or ():
                if not isinstance(rule, dict) or not rule.get("key"):
                    continue
                key = str(rule["key"])
                out[(person, key)] = Lesson(
                    lesson_id="st_" + digest("stated", person, key), scope="person",
                    who=person, kind="decision" if key.startswith(RULE_PREFIXES) else "style",
                    key=key, value=str(rule.get("value") or ""),
                    confidence=1.0, weight=1.0, total=1.0, seen=1, first_at="", last_at="",
                    evidence=[{"at": "", "ref": f"memory:{item}"}],
                    text=str(rule.get("text") or ""))
    return list(out.values())


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
        learned = cls._load_learned(directory, guard)
        stated = [l for l in _stated_lessons(directory) if guard.clear(l.who)]
        if not stated:
            return learned
        # what a person stated in their own words wins over what was learned for that key
        lessons = [l for by in learned.by.values() for l in by.values()]
        mine = {(l.who, l.key) for l in stated}
        return cls([l for l in lessons if l.scope == "role" or l.kind != "style"
                    or (l.who, l.key) not in mine] + stated,
                   [s for rows in learned.examples.values() for s in rows])

    @classmethod
    def _load_learned(cls, directory: Path, guard: "Guard") -> "Lessons":
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

    def frame(self, person: str, roles: Iterable[str], key: str,
              event: Optional[Mapping[str, Any]] = None) -> Optional[Lesson]:
        """The greeting or sign-off (``key``) for a draft to ``event``'s sender: a rule the
        person stated first, then what they write to that correspondent, then to that side
        (customers, suppliers, colleagues), then their own in general, then their role's."""
        own = self.by.get(("person", person), {})
        ev = event or {}
        sender, side = sender_key(ev.get("sender")), _slug(ev.get("side"))
        for said in ((own.get(f"style:side:{side}:{key}") if side in AUDIENCES else None),
                     own.get(f"style:{key}")):
            if said is not None and said.lesson_id.startswith("st_"):
                return said   # stated for this side first, then stated for everyone
        for scoped in ((f"sender:{sender}:{key}" if sender else None),
                       (f"side:{side}:{key}" if side in AUDIENCES else None)):
            got = own.get(f"style:{scoped}") if scoped else None
            if got is not None and got.confidence >= APPLY_AT:
                return got
        return self.get(person, roles, "style", key)

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
                                         chat=slot == "teams_draft", event=event)
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

    def restyle(self, body: str, person: str, roles: Sequence[str], *, chat: bool = False,
                event: Optional[Mapping[str, Any]] = None) -> tuple[str, list[Lesson]]:
        from . import copy_gate as gate
        if chat:
            # the lessons are learned from mail replies: a chat reply keeps its own form
            return body, []
        lines = body.split("\n")
        used: list[Lesson] = []
        idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        greet = self.frame(person, roles, "greeting", event)
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
            drops = {_drop_key(_template_from_text(l.text)): l for l in drop}
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
                keep = [x for x in sentences if _drop_key(template(x)) not in drops]
                hit += [drops[_drop_key(template(x))] for x in sentences
                        if _drop_key(template(x)) in drops]
                trimmed.append(" ".join(x.strip() for x in keep) if len(keep) < len(sentences)
                               else ln)
            # never a draft left with nothing to say: then nothing is dropped
            if hit and content(trimmed) >= 1:
                lines = [ln for i, ln in enumerate(trimmed)
                         if ln.strip() or (i and trimmed[i - 1].strip())]
                for lesson in hit:
                    if lesson not in used:
                        used.append(lesson)
        sign = self.frame(person, roles, "signoff", event)
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

    def repeats(self, person: str, *, at_least: int = REPEAT_AT, most: int = 3
                ) -> list[dict[str, Any]]:
        """The mail the person keeps sending in near the same words: replies whose shape
        (generic opening and closing sentences, ``edit_features``) is the same ``at_least``
        times, with two or more generic sentences, as a body to save as a template ({side,
        count, last_at, body}); a fact sentence is a "..." line to fill. Most sent first.
        Nothing kept of a reply's own words beyond those generic templates."""
        groups: dict[tuple[str, tuple[str, ...]], list[Signal]] = defaultdict(list)
        for sig in self.examples.get(person, ()):
            shape = tuple(str(x) for x in (sig.data.get("shape") or ()))
            generic = [x for x in shape if x != "#"]
            if len(generic) < 2 or len(generic) * 2 < len(shape):
                continue
            side = _slug(sig.data.get("side"))
            groups[(side if side in AUDIENCES else "", shape)].append(sig)
        out = []
        for (side, shape), sigs in groups.items():
            if len(sigs) < at_least:
                continue
            forms = Counter(str(x.data.get("greeting") or "none") for x in sigs)
            closes = Counter(str(x.data.get("signoff") or "none") for x in sigs)
            greet = _greeting_line(forms.most_common(1)[0][0].replace("_name", ""), None)
            close = SIGNOFF_TEXT.get(closes.most_common(1)[0][0])
            lines = []
            for x in shape:
                line = "..." if x == "#" else re.sub(r"\bi\b", "I", x)
                if not lines or line != "..." or lines[-1] != "...":
                    lines.append(line[:1].upper() + line[1:])
            body = "\n\n".join(p for p in (greet, "\n".join(lines), close) if p)
            out.append({"side": side, "count": len(sigs), "last_at": max(x.at for x in sigs),
                        "body": body, "first": next(x for x in lines if x != "...")})
        out.sort(key=lambda r: (r["count"], r["last_at"]), reverse=True)
        return out[:most]

    # -- items --

    def _stated(self, person: str, value: str) -> list[Lesson]:
        """The person's own stated Autopilot rules of one ``value`` (skip, cc, never)."""
        return [l for l in self.by.get(("person", person), {}).values()
                if l.kind == "decision" and l.lesson_id.startswith("st_")
                and l.key.startswith(RULE_PREFIXES) and l.value == value]

    def stated_skip(self, person: str, event: Mapping[str, Any]) -> Optional[Lesson]:
        """The rule of ``person``'s that says no draft for this message: its sender (or
        one of ``sender_addresses``, a Teams sender's addresses) is the rule's address or
        in its domain, or the rule covers the message's whole side."""
        side = str(event.get("side") or "")
        senders = [event.get("sender"), *(event.get("sender_addresses") or ())]
        for l in self._stated(person, "skip"):
            _kind, scope, target = l.key.split(":", 2)
            if scope != "all" and scope != side:
                continue
            if target == "*" or any(rule_matches(target, a) for a in senders):
                return l
        return None

    def recipients(self, person: str) -> dict[str, str]:
        """The addresses ``person`` said are never copied (address -> ``never``), the
        recipient lessons ``assist.resolve_reply`` honours."""
        return {l.key.split(":", 1)[1]: "never" for l in self._stated(person, "never")}

    def stated_cc(self, person: str, side: Any) -> list[str]:
        """The addresses ``person`` said to copy on their email drafts to ``side`` (or on
        every one), oldest rule first."""
        out = []
        for l in self._stated(person, "cc"):
            _kind, scope, address = l.key.split(":", 2)
            if (scope == "all" or scope == str(side or "")) and address not in out:
                out.append(address)
        return out

    def skip_reason(self, person: str, roles: Iterable[str], autopilot: str,
                    event: Mapping[str, Any]) -> Optional[str]:
        """Why an item is not prepared for ``person``: they said so (``stated_skip``:
        ``stated_left:<lesson>``), or they leave items of this kind (or from this sender) —
        confidence ``SKIP_AT`` on ``SKIP_MIN`` evidence; else None."""
        said = self.stated_skip(person, event)
        if said is not None:
            return f"stated_left:{said.lesson_id}"
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
        mine = {l.key: l for l in self.of(person)}
        for l in self.of(person):
            if l.confidence < APPLY_AT or not l.text or l.key.startswith("sender:") \
                    or l.lesson_id.startswith("st_"):
                continue   # a stated rule is already the person's own memory item
            general = mine.get(l.key.rsplit(":", 1)[1]) if l.key.startswith("side:") else None
            if general is not None and general.value == l.value \
                    and general.confidence >= APPLY_AT:
                continue   # the same greeting or sign-off for that side as for everyone
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


def _drop_key(generic: Optional[str]) -> Optional[str]:
    """A sentence template as a dropped sentence is matched: its closing stop aside."""
    return generic.rstrip(" .!?;:") or None if generic else None


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


def unlearn(directory: Optional[Path], person: str, subject: Any,
            now: Optional[datetime] = None) -> bool:
    """A learned item the person deleted from their memory (subject ``learn.<kind>.<key>``,
    as ``Lessons.memory_items`` names it) forgotten in the ledger too: the lesson stops
    shaping their drafts at once and is never learned again. False for any other subject."""
    text = str(subject or "")
    if directory is None or not text.startswith("learn."):
        return False
    kind, _, key = text[len("learn."):].partition(".")
    if kind not in LESSON_KINDS or not key or not person:
        return False
    ledger = Ledger(directory)
    try:
        return ledger.forget(person, kind, key, now or datetime.now(timezone.utc))
    finally:
        ledger.conn.close()


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
        if not args.store and (args.state_dir / "projection" / "CURRENT").is_file():
            # the served generation's store, as the autopilot reads it
            current = json.loads((args.state_dir / "projection" / "CURRENT").read_text())
            args.store = str(args.state_dir / "projection" / "generations"
                             / str(current["generation"]) / "store.sqlite")
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
