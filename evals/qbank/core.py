"""Family model, grading specs, surface variation and stratified sampling.

A ``Family`` is one kind of question: a pool SQL (real entities, with an activity weight
``w`` for strata), a truth SQL (bound with the instance's parameters), several phrasings and
a ``grade`` callable turning the truth rows into a grading spec (or None: the instance is
unusable and dropped). Nothing here names an entity or a value.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import random
import re
from typing import Any, Callable, Mapping, Sequence

KINDS = ("lookup", "count", "sum", "rank", "compare", "multi", "followup", "impossible", "trap", "mined")
VARIANTS = ("plain", "typo", "casual", "vague")
VARIANT_WEIGHTS = (0.45, 0.2, 0.2, 0.15)


@dataclasses.dataclass(frozen=True)
class Family:
    id: str
    system: str            # sales, ar, ap, purchasing, stock, quality, mission_control, ...
    type: str              # the business type the question is about
    kind: str
    pool: str              # SQL -> parameter rows (optional column w: activity, for strata)
    truth: str             # SQL bound with the parameters (+ today) -> truth rows
    asks: tuple[str, ...]  # phrasings with {param} slots; follow-ups: turns separated by " || "
    grade: Callable[[list[dict[str, Any]], Mapping[str, Any]], dict[str, Any] | None]
    n: int = 45
    truth_source: str = "biz"   # biz (curated tables) or ontology (served records)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"family {self.id}: unknown kind {self.kind!r}")


# ---- grading specs ---------------------------------------------------------------------

def D(value: Any) -> decimal.Decimal:
    return value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))


def num(value: Any, label: str, tol: float | None = None, alts: Sequence[Any] = (),
        currency: str | None = None) -> dict[str, Any]:
    """A number the answer must state. Counts are exact up to 20, then 2%; money 1%."""
    v = D(value)
    if tol is None:
        tol = 0.01 if currency else (0.0 if abs(v) <= 20 and v == v.to_integral_value() else 0.02)
    return {"value": str(v), "label": label, "tol": tol, "alts": [str(D(a)) for a in alts],
            "currency": currency}


def spec(rule: str, *, numbers: Sequence[dict] = (), min_numbers: int | None = None,
         names: Sequence[str] = (), min_names: int | None = None, dates: Sequence[Any] = (),
         min_dates: int | None = None, date_tol_days: int = 0, cannot: bool = False,
         zero_ok: bool = False, must_any: Sequence[str] = (), forbid_any: Sequence[str] = ()) -> dict[str, Any]:
    names = [[str(a) for a in n if a is not None and str(a).strip()] if isinstance(n, (list, tuple)) else str(n)
             for n in names if n is not None and str(n).strip()]
    names = [n for n in names if n]
    dates = [d.isoformat()[:10] if isinstance(d, (dt.date, dt.datetime)) else str(d)[:10] for d in dates if d]
    return {"rule": rule, "numbers": list(numbers),
            "min_numbers": len(numbers) if min_numbers is None else min_numbers,
            "names": names, "min_names": (len(names) if min_names is None else min_names) if names else 0,
            "dates": dates, "min_dates": (len(dates) if min_dates is None else min_dates) if dates else 0,
            "date_tol_days": date_tol_days, "cannot": cannot, "zero_ok": zero_ok,
            "must_any": [m for m in must_any if m], "forbid_any": list(forbid_any)}


def cannot(rule: str) -> dict[str, Any]:
    return spec(rule, cannot=True)


# ---- surface variation -----------------------------------------------------------------

ABBREV = (("purchase order", "PO"), ("sales order", "SO"), ("quantity", "qty"), ("invoice", "inv"),
          ("customer", "cust"), ("please", "pls"), ("number", "#"), ("approximately", "approx"),
          ("you", "u"), ("are", "r"), ("supplier", "vendor"), ("information", "info"))
LEGAL = re.compile(r"[,.]?\s+(inc|ltd|llc|l\.l\.c|corp|corporation|co|limited|ltée|ltee|gmbh|s\.a|sa)\.?$", re.I)
FILLER = ("hey, ", "quick one: ", "", "can you check ", "do you know ", "")
KEYS_NEAR = {c: n for c, n in zip("qwertyuiopasdfghjklzxcvbnm", "wqretyuiposadfghjkllxzvcbmn")}


def protected_tokens(params: Mapping[str, Any]) -> set[str]:
    out = set()
    for v in params.values():
        for tok in re.findall(r"\S+", str(v or "")):
            out.add(tok.lower().strip(".,?'\""))
    return out


def typo(text: str, rng: random.Random, protect: set[str]) -> str:
    words = text.split(" ")
    idx = [i for i, w in enumerate(words) if re.fullmatch(r"[A-Za-z]{5,}[?,.]?", w)
           and w.lower().strip("?,.") not in protect]
    for i in rng.sample(idx, min(len(idx), rng.choice((1, 1, 2)))):
        w = words[i]
        core, tail = re.match(r"([A-Za-z]+)(.*)", w).groups()
        j = rng.randrange(1, len(core) - 1)
        op = rng.randrange(4)
        if op == 0:
            core = core[:j] + core[j + 1] + core[j] + core[j + 2:]
        elif op == 1:
            core = core[:j] + core[j + 1:]
        elif op == 2:
            core = core[:j] + core[j] + core[j:]
        else:
            core = core[:j] + KEYS_NEAR.get(core[j].lower(), core[j]) + core[j + 1:]
        words[i] = core + tail
    return " ".join(words)


def casual(text: str, rng: random.Random, protect: set[str]) -> str:
    out = text
    for long, short in ABBREV:
        if rng.random() < 0.6:
            out = re.sub(rf"\b{long}\b", short, out, flags=re.I)
    out = " ".join(w if w.lower().strip("?,.") in protect else w.lower() for w in out.split(" "))
    if rng.random() < 0.5:
        out = out.rstrip("?").rstrip()
    return rng.choice(FILLER) + out


def vague(text: str, params: Mapping[str, Any], rng: random.Random, protect: set[str]) -> str:
    """Looser wording: legal suffixes dropped from names, formal words dropped, casual."""
    out = text
    for v in params.values():
        if isinstance(v, str) and LEGAL.search(v.strip()):
            out = out.replace(v, LEGAL.sub("", v.strip()))
    out = re.sub(r"\b(the record|the document|currently|exactly|in total|right now)\b ?", "", out, flags=re.I)
    return casual(re.sub(r"\s{2,}", " ", out).strip(), rng, protect)


def surface(asks: Sequence[str], params: Mapping[str, Any], rng: random.Random) -> tuple[str, str]:
    """(question text, variant) for one instance; follow-up turns stay joined by ' || '."""
    ask = rng.choice(list(asks))
    text = ask.format(**{k: _show(v) for k, v in params.items()})
    variant = rng.choices(VARIANTS, VARIANT_WEIGHTS)[0]
    protect = protected_tokens(params)
    turns = text.split(" || ")
    if variant == "typo":
        turns = [typo(t, rng, protect) for t in turns]
    elif variant == "casual":
        turns = [casual(t, rng, protect) for t in turns]
    elif variant == "vague":
        turns = [vague(t, params, rng, protect) for t in turns]
    return " || ".join(turns), variant


def _show(v: Any) -> str:
    if isinstance(v, dt.datetime):
        return v.strftime("%B %-d, %Y")
    if isinstance(v, dt.date):
        return v.strftime("%B %-d, %Y")
    return str(v)


# ---- sampling ----------------------------------------------------------------------------

def stratified(rows: Sequence[Mapping[str, Any]], n: int, rng: random.Random, strata: int = 3) -> list[Mapping[str, Any]]:
    """n rows spread over activity strata (column w; lowest stratum = rare entities)."""
    rows = list(rows)
    if len(rows) <= n:
        return rows
    rows.sort(key=lambda r: (float(r.get("w") or 0), json.dumps(_key(r), sort_keys=True, default=str)))
    size = -(-len(rows) // strata)
    bins = [rows[i:i + size] for i in range(0, len(rows), size)]
    for b in bins:
        rng.shuffle(b)
    out: list[Mapping[str, Any]] = []
    while len(out) < n:
        for b in bins:
            if b and len(out) < n:
                out.append(b.pop())
    return out


def _key(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k != "w"}


def instance_id(family: Family, params: Mapping[str, Any]) -> str:
    blob = json.dumps(_key(params), sort_keys=True, default=str)
    return f"{family.id}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def split_of(instance: str, holdout_share: int = 20) -> str:
    """HOLDOUT for holdout_share percent of instances, by hash; every phrasing of one instance
    (and every turn of a follow-up) lands on the same side."""
    h = int(hashlib.sha256(("qbank-split/" + instance).encode()).hexdigest()[:8], 16)
    return "holdout" if h % 100 < holdout_share else "tune"
