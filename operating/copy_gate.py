"""The slop gate: one check for every product string, alert summary and drafted message.

``check(text, surface=..., facts=..., voice=...)`` returns ``{"ok", "findings"}``; each
finding is ``{"rule", "start", "end", "detail"}``. The rules are data in
``copy_rules.json`` (shared with the app's copy test) plus the shape rules below:

* ``banned_phrase``, ``banned_char``, ``synonym``: from the rules file, by surface;
* ``paren_plural``: "order(s)" (a plural is computed: ``count``);
* ``iso_date_in_prose``: ``2026-11-12`` outside a handle or a record id (write ``say_date``);
* ``hedge_stack``: two hedges in one sentence, or a hedge in a summary with no reason
  (``because`` or ``:``) in the same sentence;
* ``too_long``: the surface's limit (``limits``), and a draft body over the sender's p75;
* ``missing_specific``: a summary or subject without a record number from ``facts``, a
  summary or body without one number or date from ``facts``;
* ``unsupported_number`` / ``unsupported_date``: a number or date the facts do not state
  (nor today, nor a fact rounded); reuses the answer gate's readers (``claim_checks``);
* ``voice_greeting`` / ``voice_signoff``: a draft's first and last lines must be shapes the
  sender uses (company profile by default), and a greeting names a person, never a firm.

The fact rules run only when ``facts`` are given (every generation-time call passes the
item's cited facts). ``settle`` is the generation-time path: a deterministic text with an
optional sentence that fails loses that sentence; a required one that fails returns None.
Model-written text gets one regeneration with the findings, then the same drop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import claim_checks as cc

RULES_PATH = Path(__file__).with_name("copy_rules.json")
SURFACES = ("ui", "summary", "why", "teams", "draft_subject", "draft_body")
GROUP = {"ui": "ui", "summary": "summary", "why": "summary", "teams": "summary",
         "draft_subject": "draft", "draft_body": "draft"}
PROSE = frozenset({"summary", "why", "teams", "draft_subject", "draft_body"})
CODES = ("banned_phrase", "banned_char", "synonym", "paren_plural", "iso_date_in_prose",
         "hedge_stack", "too_long", "missing_specific", "unsupported_number",
         "unsupported_date", "voice_greeting", "voice_signoff")

Fact = tuple[str, Any]

_PAREN_PLURAL = re.compile(r"\b\w+\((?:s|es)\)", re.I)
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# a sentence ends at "." or "?" before a space or the end ("38.5%" and "1,200.50" stay whole)
_SENTENCE = re.compile(r"(?:[^.?\n]|[.?](?=[^\s.?]))+(?:[.?]+(?=\s|$)|$)")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_SHORT_DATE = re.compile(r"\b(?P<d>\d{1,2}) (?P<m>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|"
                         r"Dec)[a-z]*\b(?! \d{4})")
_GREETING = re.compile(r"^(?P<word>hi|hello|hey|dear|good (?:morning|afternoon|evening))\b"
                       r"\s*(?P<name>[^,]*?),?\s*$", re.I)
_SIGNOFFS = {"thank you": "thank_you", "thank you.": "thank_you", "thank you,": "thank_you",
             "thanks": "thanks", "thanks,": "thanks", "thanks.": "thanks",
             "regards": "regards", "regards,": "regards", "best": "best", "best,": "best",
             "best regards": "regards", "best regards,": "regards", "kind regards,": "regards",
             "kind regards": "regards", "cheers": "cheers", "cheers,": "cheers",
             "sincerely": "sincerely", "sincerely,": "sincerely",
             "thank you for your patience,": "thank_you"}
_FIRM = re.compile(r"\b(?:inc|ltd|llc|limited|corp|corporation|co|gmbh|plc|sa|ag|bv|"
                   r"company|group|industries|team|labs?)\b\.?", re.I)
_PARTY_HANDLES = ("party", "supplier", "customer", "vendor", "organisation", "organization",
                  "company", "firm")


def rules() -> dict[str, Any]:
    if not hasattr(rules, "_cache"):
        rules._cache = json.loads(RULES_PATH.read_text(encoding="utf-8"))  # type: ignore
    return rules._cache  # type: ignore[attr-defined]


@dataclass(frozen=True)
class VoiceProfile:
    """A sender's measured voice as categories and numbers only (never text)."""
    greetings: tuple[str, ...] = ("hi", "none")
    signoffs: tuple[str, ...] = ("none",)
    p50_words: int = 23
    p75_words: int = 47

    @classmethod
    def company(cls) -> "VoiceProfile":
        v = rules()["voice"]
        return cls(tuple(v["greetings"]), tuple(v["signoffs"]), int(v["p50_words"]),
                   int(v["p75_words"]))


def _applies(rule: Mapping[str, Any], surface: str) -> bool:
    return GROUP[surface] in rule.get("applies", ()) or surface in rule.get("applies", ())


def _find(findings: list, rule: str, start: int, end: int, detail: str) -> None:
    findings.append({"rule": rule, "start": start, "end": end, "detail": detail})


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.replace(",", "").strip())
        except InvalidOperation:
            return None
    return None


def _as_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO.fullmatch(value[:10]):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _record_numbers(facts: Sequence[Fact]) -> list[str]:
    """Fact values that name a record: text holding a digit that is not a date or amount."""
    out = []
    for _handle, value in facts:
        if isinstance(value, str) and re.search(r"\d", value) and _as_day(value) is None \
                and _decimal(value) is None:
            out.append(value)
        elif isinstance(value, str) and _decimal(value) is not None and \
                str(_handle).lower() in ("n", "number", "record", "po", "order", "ref", "id"):
            out.append(value)
    return out


def _fact_numbers(facts: Sequence[Fact]) -> list[Decimal]:
    out = []
    for _handle, value in facts:
        number = _decimal(value)
        if number is not None:
            out.append(number)
        elif isinstance(value, str):
            out += [n for _w, n in cc.prose_numbers(value)]
            out += [Decimal(m) for m in re.findall(r"\d+", value)]
    return out


def _fact_days(facts: Sequence[Fact]) -> set[date]:
    return {d for _h, v in facts if (d := _as_day(v)) is not None}


def _strip_ids(text: str, facts: Sequence[Fact]) -> str:
    """``text`` with handles and the facts' record numbers blanked (kept the same length)."""
    text = cc.HANDLE.sub(lambda m: " " * len(m.group(0)), text)
    for ref in sorted(_record_numbers(facts), key=len, reverse=True):
        text = text.replace(ref, " " * len(ref))
    return text


def _greeting_line(lines: list[str]) -> tuple[int, str] | None:
    for n, line in enumerate(lines):
        if line.strip():
            return (n, line.strip()) if _GREETING.match(line.strip()) else None
    return None


def _signoff_line(lines: list[str]) -> tuple[int, str] | None:
    for n in range(len(lines) - 1, -1, -1):
        if lines[n].strip():
            key = lines[n].strip().lower()
            return (n, key) if key in _SIGNOFFS else None
    return None


def check(text: str, *, surface: str, facts: Sequence[Fact] = (),
          voice: VoiceProfile | None = None, today: date | None = None,
          specific: bool = True) -> dict[str, Any]:
    """The findings of every rule on ``text`` shown on ``surface`` (module doc).
    ``specific`` False: a text that deliberately states no figure (a message that must not
    give the customer our numbers) skips ``missing_specific``; the fact rules still hold."""
    if surface not in SURFACES:
        raise ValueError(f"unknown copy surface {surface!r}")
    data = rules()
    findings: list[dict[str, Any]] = []
    text = text or ""
    voice = voice or VoiceProfile.company()
    today = today or date.today()
    for rule in data["banned_phrases"]:
        if _applies(rule, surface):
            for m in re.finditer(r"(?<!\w)(?:" + rule["pattern"] + r")(?!\w)", text, re.I):
                _find(findings, "banned_phrase", m.start(), m.end(), rule["reason"])
    for rule in data["banned_chars"]:
        if _applies(rule, surface):
            for m in re.finditer(re.escape(rule["char"]), text):
                _find(findings, "banned_char", m.start(), m.end(), rule["reason"])
    for rule in data["terms"]:
        if _applies(rule, surface):
            for m in re.finditer(r"\b(?:" + rule["pattern"] + r")\b", text, re.I):
                _find(findings, "synonym", m.start(), m.end(), f"say {rule['use']}")
    for m in _PAREN_PLURAL.finditer(text):
        _find(findings, "paren_plural", m.start(), m.end(), "compute the plural")
    if surface in PROSE:
        plain = _strip_ids(text, facts)
        for m in _ISO.finditer(plain):
            _find(findings, "iso_date_in_prose", m.start(), m.end(), "write Wed 12 Nov")
    _hedges(text, surface, findings)
    _length(text, surface, voice, findings)
    if facts and surface in PROSE:
        _specifics(text, surface, facts, today, findings, specific)
    if surface == "draft_body":
        _voice(text, voice, facts, findings)
    return {"ok": not findings, "findings": findings}


def _hedges(text: str, surface: str, findings: list) -> None:
    words = rules()["hedges"]
    pattern = re.compile(r"\b(?:" + "|".join(words) + r")\b")
    for s in _SENTENCE.finditer(text):
        sentence = s.group(0)
        # "May" as the month (capitalised, a day beside it) is no hedge
        found = [m for m in pattern.finditer(sentence)]
        if len(found) > 1:
            _find(findings, "hedge_stack", s.start(), s.end(), "two hedges in one sentence")
        elif found and surface == "summary" and "because" not in sentence and \
                ":" not in sentence:
            _find(findings, "hedge_stack", s.start(), s.end(), "a hedge with no reason")


def _length(text: str, surface: str, voice: VoiceProfile, findings: list) -> None:
    limits = rules()["limits"]
    if surface == "summary" and len(text) > limits["summary_chars"]:
        _find(findings, "too_long", 0, len(text), f"over {limits['summary_chars']} characters")
    elif surface == "why" and len([s for s in _SENTENCE.findall(text) if s.strip()]) > \
            limits["why_sentences"]:
        _find(findings, "too_long", 0, len(text), f"over {limits['why_sentences']} sentences")
    elif surface == "teams" and len(text.split()) > limits["teams_words"]:
        _find(findings, "too_long", 0, len(text), f"over {limits['teams_words']} words")
    elif surface == "draft_subject" and len(text) > limits["subject_chars"]:
        _find(findings, "too_long", 0, len(text), f"over {limits['subject_chars']} characters")
    elif surface == "draft_body":
        lines = text.split("\n")
        g, s = _greeting_line(lines), _signoff_line(lines)
        # prose lines only: a list of record lines ("- ...") states facts, not words
        body = [line for n, line in enumerate(lines) if line.strip()
                and not line.lstrip().startswith("- ")
                and not (g and n == g[0]) and not (s and n == s[0])]
        words = sum(len(line.split()) for line in body)
        if len(body) > limits["draft_lines"]:
            _find(findings, "too_long", 0, len(text), f"over {limits['draft_lines']} lines")
        if words > min(voice.p75_words, limits["draft_words"]):
            _find(findings, "too_long", 0, len(text),
                  f"{words} words, over the sender's {voice.p75_words}")


def _specifics(text: str, surface: str, facts: Sequence[Fact], today: date,
               findings: list, specific: bool = True) -> None:
    refs = _record_numbers(facts)
    if specific and surface in ("summary", "draft_subject") and refs and not any(r in text for r in refs):
        _find(findings, "missing_specific", 0, len(text), "name the record number")
    numbers = _fact_numbers(facts)
    numbers += [n / 100 for n in numbers]   # a percent is read at its fraction scale
    days = _fact_days(facts) | {today}
    plain = _strip_ids(text, facts)
    found_any = any(r in text for r in refs)
    for written, number in cc.prose_numbers(plain):
        start = plain.find(written)
        if number in numbers or cc.rounded_from(number, numbers) or \
                (number == today.day):
            found_any = True
            continue
        _find(findings, "unsupported_number", max(start, 0), max(start, 0) + len(written),
              f"{written} is not a cited fact")
    for written, moment, _iso in cc.written_dates(plain):
        day = _as_day(moment[0]) if moment and isinstance(moment[0], str) else None
        start = plain.find(written)
        if day in days:
            found_any = True
        else:
            _find(findings, "unsupported_date", max(start, 0), max(start, 0) + len(written),
                  f"{written} is not a cited date")
    for m in _SHORT_DATE.finditer(plain):
        key = (int(m["d"]), _MONTHS.index(m["m"][:3].lower()) + 1)
        if key in {(d.day, d.month) for d in days}:
            found_any = True
        else:
            _find(findings, "unsupported_date", m.start(), m.end(),
                  f"{m.group(0)} is not a cited date")
    if specific and surface in ("summary", "draft_body") and (numbers or len(days) > 1) and not found_any:
        _find(findings, "missing_specific", 0, len(text), "state a cited number or date")


def _voice(text: str, voice: VoiceProfile, facts: Sequence[Fact], findings: list) -> None:
    lines = text.split("\n")
    g = _greeting_line(lines)
    if g is None:
        if "none" not in voice.greetings:
            _find(findings, "voice_greeting", 0, 0, "the sender opens with a greeting")
    else:
        m = _GREETING.match(g[1])
        word = m["word"].lower().split()[0]
        name = (m["name"] or "").strip()
        parties = {str(v).strip().lower() for h, v in facts
                   if any(p in str(h).lower() for p in _PARTY_HANDLES)}
        start = text.find(g[1])
        if word not in voice.greetings:
            _find(findings, "voice_greeting", start, start + len(g[1]),
                  f"the sender does not open with {m['word']!r}")
        elif name and (len(name.split()) > 1 or _FIRM.search(name)
                       or name.lower() in parties):
            _find(findings, "voice_greeting", start, start + len(g[1]),
                  "greet a person by first name, never a firm")
    s = _signoff_line(lines)
    if s is not None and _SIGNOFFS[s[1]] not in voice.signoffs:
        start = text.rfind(lines[s[0]])
        _find(findings, "voice_signoff", start, start + len(lines[s[0]]),
              "not the sender's sign-off")


# --- generation time ---------------------------------------------------------------

def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip()]


def settle(parts: Sequence[str], *, surface: str, facts: Sequence[Fact] = (),
           required: int = 1, voice: VoiceProfile | None = None, joiner: str = " ",
           today: date | None = None) -> tuple[str | None, dict[str, Any]]:
    """Deterministic text: ``parts`` joined, the optional ones (after the first
    ``required``) dropped while they fail; None when a required part fails. Returns
    ``(text or None, copy_check)``."""
    kept = list(parts)
    result = check(joiner.join(kept), surface=surface, facts=facts, voice=voice, today=today)
    while not result["ok"] and len(kept) > required:
        bad = _failing(kept, surface, facts, voice, joiner, today)
        if bad is None or bad < required:
            break
        kept.pop(bad)
        result = check(joiner.join(kept), surface=surface, facts=facts, voice=voice,
                       today=today)
    return (joiner.join(kept) if result["ok"] else None), result


def _failing(parts: list[str], surface: str, facts: Sequence[Fact],
             voice: VoiceProfile | None, joiner: str, today: date | None) -> int | None:
    """The last part a finding falls in (a length finding: the last optional part)."""
    text = joiner.join(parts)
    findings = check(text, surface=surface, facts=facts, voice=voice, today=today)["findings"]
    bounds, at = [], 0
    for part in parts:
        bounds.append((at, at + len(part)))
        at += len(part) + len(joiner)
    hit = None
    for f in findings:
        for n, (a, b) in enumerate(bounds):
            if f["end"] > a and f["start"] < b and (f["end"] - f["start"]) < len(text):
                hit = n if hit is None else max(hit, n)
    if hit is None and findings:
        hit = len(parts) - 1
    return hit


def settle_model(text: str, *, surface: str, facts: Sequence[Fact] = (),
                 regenerate: Callable[[list[dict[str, Any]]], str] | None = None,
                 voice: VoiceProfile | None = None) -> tuple[str | None, dict[str, Any]]:
    """Model-written text: one regeneration with the findings appended, then the failing
    sentences dropped; None when nothing passes (never ship a failing text)."""
    result = check(text, surface=surface, facts=facts, voice=voice)
    if not result["ok"] and regenerate is not None:
        text = regenerate(result["findings"]) or ""
        result = check(text, surface=surface, facts=facts, voice=voice)
    if result["ok"]:
        return text, result
    kept = [s for s in sentences(text)
            if check(s, surface=surface, facts=facts, voice=voice)["ok"]]
    if not kept:
        return None, result
    joined = " ".join(kept)
    final = check(joined, surface=surface, facts=facts, voice=voice)
    return (joined if final["ok"] else None), final


# --- words the gate asks for ---------------------------------------------------------

_IRREGULAR = {"business day": "business days", "batch": "batches", "box": "boxes",
              "entry": "entries", "delivery": "deliveries", "quantity": "quantities",
              "inquiry": "inquiries", "company": "companies", "reply": "replies"}


def count(n: int | Decimal, noun: str, plural: str | None = None) -> str:
    """'1 order', '3 orders': a computed plural, never 'order(s)'."""
    many = plural or _IRREGULAR.get(noun) or (noun + "s")
    return f"{n} {noun if n == 1 else many}"


def say_date(value: Any, today: date | None = None) -> str:
    """'Wed 12 Nov' (the year only outside the current one); the input as given when it
    is no date."""
    day = _as_day(value)
    if day is None:
        return str(value)
    today = today or date.today()
    text = f"{day:%a} {day.day} {day:%b}"
    return text if day.year == today.year else f"{text} {day.year}"


def facts_of(mapping: Mapping[str, Any] | Iterable[Fact]) -> list[Fact]:
    items = mapping.items() if isinstance(mapping, Mapping) else mapping
    return [(str(k), v) for k, v in items if v is not None and not isinstance(v, (list, dict))]
