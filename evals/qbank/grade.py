"""Deterministic grading of an Ask answer against an item's grading spec.

Rules: numbers within tolerance (Decimal; dates are blanked before numbers are read, so a
date's digits never pass as a figure); a stated currency must not contradict the truth's
currency (the right figure in the wrong currency fails); names (each entry a list of acceptable spellings) and dates (within
``date_tol_days``) need their minimum hits; ``zero_ok`` accepts a stated zero or "none";
``cannot`` needs the answer to say it cannot find or determine it (or an abstained /
not-found outcome) and states no figure in its place; ``must_any`` needs one of its words.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import unicodedata
from typing import Any, Mapping

NUM = re.compile(r"(?<![\w.])-?\(?[$€£]?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(k|K|M|million|thousand|bn|billion)?(?![\w])")
SUFFIX = {"k": 10 ** 3, "K": 10 ** 3, "thousand": 10 ** 3, "M": 10 ** 6, "million": 10 ** 6, "bn": 10 ** 9, "billion": 10 ** 9}
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
MDY_WORD = re.compile(rf"\b{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*(?:[-–—]|to)\s*\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s+(\d{{4}}))?", re.I)
DMY_WORD = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MON}(?:,?\s+(\d{{4}}))?", re.I)
SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
REFUSAL = re.compile(
    r"\b(couldn'?t|could not|can'?t|cannot|unable to|no (?:such|record|records|match|matching|data|information|results?|way to)|"
    r"not (?:find|found|able|possible|in (?:our|the) (?:records|system|data))|doesn'?t (?:exist|appear)|does not (?:exist|appear)|"
    r"don'?t have|do not have|isn'?t (?:in|any)|there (?:is|are) no|nothing (?:matches|found|on (?:file|record))|"
    r"impossible to|can'?t (?:be )?(?:known|predicted|determined)|not (?:yet )?(?:known|available|determinable))\b", re.I)
CURRENCY = {"USD": ("usd", "us$", "us dollar", "u.s. dollar", "us dollars"), "CAD": ("cad", "c$", "ca$", "canadian")}
GENERIC = {"inc", "ltd", "llc", "the", "of", "co", "corp", "corporation", "company", "limited", "ltee", "dba"}


def norm(s: Any) -> str:
    s = unicodedata.normalize("NFKC", str(s)).replace("\xa0", " ").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def answer_parts(ans: Mapping[str, Any]) -> str:
    parts = [str(ans.get("text") or ans.get("direct_answer") or "")]
    parts += [str(f.get("text") or "") for f in ans.get("facts") or ()]
    for k in ("clarify", "not_found", "assumption"):
        if ans.get(k):
            parts.append(ans[k] if isinstance(ans[k], str) else json.dumps(ans[k]))
    return "\n".join(p for p in parts if p)


def dates_in(text: str, default_year: int | None) -> list[dt.date]:
    out = []
    def add(y, m, d):
        try:
            out.append(dt.date(int(y), int(m), int(d)))
        except (TypeError, ValueError):
            pass
    for m in ISO.finditer(text):
        add(*m.groups())
    for m in MDY_WORD.finditer(text):
        add(m.group(3) or default_year, MONTHS[m.group(1)[:3].lower()], m.group(2))
    for m in DMY_WORD.finditer(text):
        add(m.group(3) or default_year, MONTHS[m.group(2)[:3].lower()], m.group(1))
    for m in SLASH.finditer(text):
        a, b, y = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        add(y, a, b)
        add(y, b, a)
    return out


def strip_dates(text: str) -> str:
    for rx in (ISO, MDY_WORD, DMY_WORD, SLASH):
        text = rx.sub(" ", text)
    return re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b", " ", text, flags=re.I)


def numbers_in(text: str) -> list[tuple[decimal.Decimal, decimal.Decimal]]:
    """(value, half its last stated digit) for each figure written in the text."""
    out = []
    for m in NUM.finditer(strip_dates(text)):
        whole, frac, suf = m.group(1).replace(",", ""), m.group(2) or "", m.group(3)
        v = decimal.Decimal(whole + frac)
        half = decimal.Decimal(1).scaleb(-len(frac[1:])) / 2 if frac else decimal.Decimal("0.5")
        if suf:
            v *= SUFFIX[suf]
            half *= SUFFIX[suf]
        out.append((v, half))
    return out


def num_ok(spec: Mapping[str, Any], found) -> bool:
    for target in [spec["value"]] + list(spec.get("alts") or ()):
        t = abs(decimal.Decimal(str(target)))
        slack = max(decimal.Decimal(str(spec.get("tol") or 0)) * t, decimal.Decimal("0.005"))
        for v, half in found:
            if abs(abs(v) - t) <= max(slack, min(half, t * decimal.Decimal("0.05") + decimal.Decimal("0.5"))):
                return True
    return False


def name_ok(name: str, t: str) -> bool:
    n = norm(re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", str(name), flags=re.I))
    if not n:
        return False
    if len(n) > 40:
        n = n[:40].rsplit(" ", 1)[0]
    if f" {n} " in t:
        return True
    toks = [w for w in n.split() if w not in GENERIC]
    key = " ".join(toks[:2]) if toks else n
    return len(key) >= 3 and f" {key} " in t


def currency_contradicts(code: str | None, low: str) -> bool:
    if not code or code not in CURRENCY:
        return False
    said = {c for c, words in CURRENCY.items() if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low) for w in words)}
    return bool(said) and code not in said


def grade(item: Mapping[str, Any], ans: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    g = item["grading"]
    if not ans or ans.get("error") and not ans.get("text"):
        return False, ["no answer" + (f": {ans.get('error')}" if ans else "")]
    raw = answer_parts(ans)
    low = raw.lower()
    t = " " + norm(raw) + " "
    why: list[str] = []
    refused = bool(REFUSAL.search(raw)) or ans.get("outcome") in ("not_found", "abstained")
    if g.get("cannot"):
        return (True, []) if refused else (False, ["did not say it cannot be found/determined"])
    found = numbers_in(raw)
    nums = list(g.get("numbers") or ())
    if g.get("zero_ok"):
        zero = re.search(r"\b(nothing|none|zero|no (?:open|outstanding|overdue|past[- ]due)\b|no balance|doesn'?t owe|does not owe|"
                         r"not owe|fully paid|there (?:are|is|were|was) no|no \w+ (?:were|was|are|is)|(?:found|has|have|had|shows?|returned) no)", low) or \
            num_ok({"value": "0", "tol": 0}, found) or refused
        if not zero:
            why.append("zero/none not stated")
        nums = []
    hit = sum(num_ok(s, found) for s in nums)
    if nums and hit < int(g.get("min_numbers", len(nums))):
        why.append(f"numbers {hit}/{g.get('min_numbers')} ({', '.join(s['label'] for s in nums if not num_ok(s, found))[:120]})")
    for s in nums:
        if currency_contradicts(s.get("currency"), low):
            why.append(f"currency differs from {s['currency']}")
            break
    names = list(g.get("names") or ())
    if names:
        hits = sum(any(name_ok(alt, t) for alt in (n if isinstance(n, list) else [n])) for n in names)
        if hits < int(g.get("min_names", 1)):
            why.append(f"names {hits}/{g.get('min_names')}")
    dates = [dt.date.fromisoformat(d) for d in g.get("dates") or ()]
    if dates:
        said = dates_in(raw, dates[0].year)
        tol = int(g.get("date_tol_days") or 0)
        hits = sum(any(abs((s - d).days) <= tol for s in said) for d in dates)
        if hits < int(g.get("min_dates", len(dates))):
            why.append(f"dates {hits}/{g.get('min_dates')}")
    if g.get("must_any") and not any(f" {norm(w)} " in t or norm(w) in t for w in g["must_any"]):
        why.append("missing: " + "/".join(g["must_any"][:4]))
    if g.get("forbid_any") and any(norm(w) in t for w in g["forbid_any"]):
        why.append("forbidden claim present")
    if not why and refused and not (nums or names or dates or g.get("must_any") or g.get("zero_ok")):
        why.append("refused")
    return not why, why
