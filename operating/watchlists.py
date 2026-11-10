"""Watchlists and alert rules: a signed-in person follows records and writes alert rules in
plain words, and gets alerts in their own inbox when what they watch changes.

* **Follow** a record (a customer, supplier, order, PO, product: any record type): an alert
  when the record itself changes (status, dates, amounts: any property but bookkeeping
  stamps), when a record linked to it changes, and when a new record is linked to it (a
  new mail or Teams message mentioning it, a new shipment or invoice) or a link goes away.
* **Rules** in plain words ("tell me when any order over $50k slips", "alert me if a
  supplier is late twice in a month") are compiled into a restricted rule spec
  (``RULE_SCHEMA``: a declared subject type, at most ``MAX_CONDITIONS`` comparisons over
  its declared properties, an optional count within a window per linked record). The
  spec is data, never code: ``validate`` refuses any key, operator, type, property or
  value outside the grammar with a plain reason, and a rule that reads marked figures
  (finance amounts, purchasing prices) from a reader who does not hold those markings.
  ``compile_text`` is a deterministic grammar; a model (``model=``, the existing
  ``claude -p`` path) may propose a spec at creation time only, which goes through the
  same ``validate``. The detector loop never calls a model.
* **Signal rules** over the detectors' own findings ("tell me when a duplicate supplier
  invoice over $10k is found", "alert me on customer credits rising"): a spec
  ``{"schema", "signal": {"kinds", "min_amount"?, "currency"?, "priority"?}}`` naming
  declared finding kinds (``signal_kinds``: the briefing's kinds, matched by their own
  names and labels, never a phrase list), an amount floor on the finding's own cited
  figure and a lowest priority. Nothing is evaluated for it in a pass: it is read from
  the engine's inbox at the moment its owner looks (``signal_alerts``), under their grant
  (``proactive_engine.read_inbox``: audiences, markings and the people exclusion per
  citation), bounded by ``SIGNALS_SHOWN``, findings first seen since the rule was made
  only. A finding whose figure the reader may not open never meets an amount floor
  (fail closed); a rule over a finance kind or an amount needs ``finance`` held.

Per-person state (follows, rules, alerts, read marks) lives in our own file beside the
served store (``watch_path``), never in a source system. ``evaluate`` runs inside the
incremental proactive pass (``operating.proactive_incremental.run_pass``) over the records
the delta layers changed: it keeps its own snapshot of every watched record's values and
links (``seen``), so a change is told against what was last seen, and writes alerts for
each owner. A follow or a rule is baselined on the first pass after it is made (no alert
for what it found then).

**Permissions**: an alert is stored with the audience key, markings and source handles of
every record it rests on (the changed record, the followed one, every counted record of a
windowed rule) and the markings of every value it states. ``alerts_for`` shows a person an
alert only when their grant, read at that moment, admits every one of those records and
markings and the per-item decision (the people exclusion) refuses none of its handles;
a stated value whose markings they do not hold is dropped, and an alert with nothing left
to state is not shown. A failing check hides the alert (fail closed).

Evidence label of the tests: ``isolated-fixture``.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import watchlists as _WATCHLISTS

RULE_SCHEMA = "plx-alert-rule/v1"
WATCH_SCHEMA = "plx-watchlists/v1"
MAX_CONDITIONS = 4
MAX_RULES_PER_READER = 50
MAX_FOLLOWS_PER_READER = 500
MAX_TEXT = 300
MAX_SPEC_BYTES = 4000
MAX_COUNT, MAX_WINDOW_DAYS = 20, 366
NEIGHBOR_CAP = 5000          # a followed record's linked records kept in its snapshot
RULE_BASELINE_CAP = 600_000  # a rule over a type with more records: refused at baseline
BASELINE_SECONDS = 20.0      # a pass's time for new rules' first look, all rules together
BASELINE_PAGE = 5000         # records read per step of a first look
ALERTS_SHOWN = 200
MAX_SIGNAL_KINDS = 5
SIGNALS_SHOWN = 200          # newest findings since a reader's oldest signal rule, per look
PRIORITIES = ("high", "medium", "low")
CHUNK = 500

CHANGE_OPS = ("changed", "later", "earlier", "increased", "decreased", "becomes")
COMPARE_OPS = ("gt", "gte", "lt", "lte", "eq", "ne")
OPS = CHANGE_OPS + COMPARE_OPS
_DATES = ("date", "timestamp")
_NUMBERS = ("money", "decimal", "integer", "quantity")
_WORDS = ("text", "enum", "identifier", "boolean")
OPS_FOR = {
    **{k: ("changed", "later", "earlier", "gt", "gte", "lt", "lte") for k in _DATES},
    **{k: ("changed", "increased", "decreased", "gt", "gte", "lt", "lte") for k in _NUMBERS},
    **{k: ("changed", "becomes", "eq", "ne") for k in _WORDS},
}
OP_WORDS = {"changed": "changes", "later": "moves later", "earlier": "moves earlier",
            "increased": "goes up", "decreased": "goes down", "becomes": "becomes",
            "gt": "is over", "gte": "is at least", "lt": "is under", "lte": "is at most",
            "eq": "is", "ne": "is not"}
# bookkeeping stamps every sync rewrites: never a change a person is told about
_STAMP = re.compile(r"(?:^|_)(?:updated|modified|created|synced|ingested|recorded)_at$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:%@/+=-]{0,400}$")
_RULE_ID = re.compile(r"^r_[0-9a-f]{16}$")
_LABEL_PROPS = ("name", "order_number", "po_number", "invoice_number", "number", "code",
                "document_number", "title", "subject")


class RuleRefused(ValueError):
    """A rule that cannot be made; ``problems`` are plain sentences."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# --------------------------------------------------------------------------- schema

@dataclass(frozen=True)
class Vocabulary:
    """The declared ontology a rule may name: type -> property -> (value type, markings),
    and link id -> (from type, to type)."""

    types: Mapping[str, Mapping[str, tuple[str, frozenset[str]]]]
    links: Mapping[str, tuple[str, str]]

    @classmethod
    def of(cls, compiled: Any) -> "Vocabulary":
        types = {tid: {p["name"]: (p.get("value_type") or "text",
                                   frozenset(p.get("markings") or ()))
                       for p in (t.get("properties") or ()) if p.get("name")}
                 for tid, t in compiled.object_types.items()}
        links = {lid: (lt.get("from"), lt.get("to")) for lid, lt in compiled.link_types.items()}
        return cls(types, links)

    def label_prop(self, object_type: str) -> list[str]:
        props = self.types.get(object_type) or {}
        return [p for p in _LABEL_PROPS if p in props and not props[p][1]]


_VOCAB: list[Vocabulary] = []
_VOCAB_LOCK = threading.Lock()


def vocabulary() -> Vocabulary:
    """The schema's vocabulary, loaded once per process."""
    with _VOCAB_LOCK:
        if not _VOCAB:
            from ontology.schema import load_schema
            _VOCAB.append(Vocabulary.of(load_schema()))
        return _VOCAB[0]


# --------------------------------------------------------------------------- validation

# a plain decimal number: at most 15 digits before the point and 10 after, an exponent of
# at most 2 digits; anything else (1e999999999, Infinity, NaN, 0x10) is not a number here,
# so no arithmetic ever sees an exponent that overflows the decimal context
_NUMBER = re.compile(r"^[+-]?(?:\d{1,15}(?:\.\d{0,10})?|\.\d{1,10})(?:[eE][+-]?\d{1,2})?$")


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None   # floats are refused: money is never a binary fraction
    text = str(value).strip().replace(",", "") if isinstance(value, str) else str(value)
    if len(text) > 40 or not _NUMBER.match(text):
        return None
    try:
        number = Decimal(text)
        if not number.is_finite() or abs(number) >= Decimal("1e12"):
            return None
    except (InvalidOperation, ArithmeticError, ValueError):
        return None
    return number


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 100 or any(ord(c) < 32 or c in "{}<>`$\\" for c in text):
        return None
    return text


def validate(spec: Any, vocab: Vocabulary, held: Iterable[str] | None) -> dict[str, Any]:
    """The rule spec, normalized, or ``RuleRefused`` with every plain reason. ``held``:
    the markings the rule's owner holds (None: not checked, the grammar only)."""
    problems: list[str] = []
    if not isinstance(spec, Mapping):
        raise RuleRefused(["A rule must be a rule spec object."])
    try:
        size = len(json.dumps(spec, sort_keys=True))
    except (TypeError, ValueError):
        raise RuleRefused(["The rule holds something that is not plain data."]) from None
    if size > MAX_SPEC_BYTES:
        raise RuleRefused(["The rule is too long."])
    if "signal" in spec:
        return _validate_signal(spec, held)
    extra = sorted(set(spec) - {"schema", "subject", "when", "count"})
    if extra:
        raise RuleRefused([f"A rule cannot hold '{k}'." for k in extra[:5]])
    if spec.get("schema") != RULE_SCHEMA:
        raise RuleRefused([f"The rule must be a {RULE_SCHEMA} spec."])
    subject = spec.get("subject")
    props = vocab.types.get(subject) if isinstance(subject, str) else None
    if props is None:
        raise RuleRefused([f"'{str(subject)[:60]}' is not a record type of the ontology."])
    when = spec.get("when")
    if not isinstance(when, list) or not 1 <= len(when) <= MAX_CONDITIONS:
        raise RuleRefused([f"A rule needs 1 to {MAX_CONDITIONS} conditions."])
    out_when: list[dict[str, Any]] = []
    markings: set[str] = set()
    for n, cond in enumerate(when, 1):
        if not isinstance(cond, Mapping):
            problems.append(f"Condition {n} is not a condition.")
            continue
        extra = sorted(set(cond) - {"property", "op", "value", "currency"})
        if extra:
            problems.append(f"Condition {n} cannot hold '{extra[0]}'.")
            continue
        prop, op = cond.get("property"), cond.get("op")
        if not isinstance(prop, str) or prop not in props:
            problems.append(f"'{str(prop)[:60]}' is not a property of {subject}.")
            continue
        kind, marks = props[prop]
        if kind not in OPS_FOR:
            problems.append(f"{subject}.{prop} ({kind}) cannot be compared.")
            continue
        if op not in OPS_FOR[kind]:
            problems.append(f"'{str(op)[:30]}' cannot be used on {subject}.{prop} ({kind}).")
            continue
        markings |= marks
        norm: dict[str, Any] = {"property": prop, "op": op}
        needs_value = op in COMPARE_OPS or op == "becomes"
        if not needs_value:
            if "value" in cond or "currency" in cond:
                problems.append(f"Condition {n} ('{op}') takes no value.")
                continue
        elif kind in _DATES:
            if not (isinstance(cond.get("value"), str) and _DAY.match(cond["value"])):
                problems.append(f"Condition {n} needs a day (YYYY-MM-DD).")
                continue
            try:
                date.fromisoformat(cond["value"])
            except ValueError:
                problems.append(f"Condition {n} needs a real day.")
                continue
            norm["value"] = cond["value"]
        elif kind in _NUMBERS:
            number = _decimal(cond.get("value"))
            if number is None:
                problems.append(f"Condition {n} needs a plain number under a trillion "
                                "(such as 50000 or 49999.99), written as text or a whole "
                                "number.")
                continue
            norm["value"] = str(number)
            currency = cond.get("currency")
            if currency is not None:
                if kind != "money" or not (isinstance(currency, str)
                                           and _CURRENCY.match(currency)):
                    problems.append(f"Condition {n}: a currency is a 3-letter code on an "
                                    "amount only.")
                    continue
                norm["currency"] = currency
        else:
            if kind == "boolean":
                if not isinstance(cond.get("value"), bool):
                    problems.append(f"Condition {n} needs true or false.")
                    continue
                norm["value"] = cond["value"]
            else:
                text = _clean_text(cond.get("value"))
                if text is None:
                    problems.append(f"Condition {n} needs a short plain word or phrase.")
                    continue
                norm["value"] = text
        out_when.append(norm)
    count = spec.get("count")
    out_count = None
    if count is not None:
        if not isinstance(count, Mapping) or set(count) - {"at_least", "within_days", "per"}:
            problems.append("A count holds at_least, within_days and per only.")
        else:
            least, days, per = count.get("at_least"), count.get("within_days"), count.get("per")
            if isinstance(least, bool) or not isinstance(least, int) \
                    or not 2 <= least <= MAX_COUNT:
                problems.append(f"A count is 2 to {MAX_COUNT} times.")
            if isinstance(days, bool) or not isinstance(days, int) \
                    or not 1 <= days <= MAX_WINDOW_DAYS:
                problems.append(f"A window is 1 to {MAX_WINDOW_DAYS} days.")
            if per is not None and (not isinstance(per, str) or per not in vocab.links
                                    or vocab.links[per][0] != subject):
                problems.append(f"'{str(per)[:60]}' is not a link from {subject}.")
            out_count = {"at_least": least, "within_days": days,
                         **({"per": per} if per is not None else {})}
    if problems:
        raise RuleRefused(problems)
    if not any(c["op"] in CHANGE_OPS for c in out_when) and out_count is not None:
        raise RuleRefused(["A counted rule needs a change to count (moves later, goes up, "
                           "becomes ...)."])
    if held is not None:
        from ontology import store_ddl
        missing = sorted(markings - set(store_ddl.with_implied(held)))
        if missing:
            names = sorted({c["property"] for c in out_when if props[c["property"]][1] - set(
                store_ddl.with_implied(held))})
            raise RuleRefused([f"This rule reads {', '.join(missing)} figures "
                               f"({', '.join(names)}); only readers cleared for "
                               f"{', '.join(missing)} may set it."])
    out = {"schema": RULE_SCHEMA, "subject": subject, "when": out_when}
    if out_count is not None:
        out["count"] = out_count
    return out


def signal_kinds() -> dict[str, dict[str, Any]]:
    """Every finding kind the engine raises that a signal rule may name: kind -> its label
    (the briefing's) and whether its figures are finance-marked (its playbook's)."""
    from . import alert_playbooks, briefing
    return {kind: {"label": briefing.EVENT_TYPES[name]["label"],
                   "finance": bool((alert_playbooks.PLAYBOOKS.get(kind) or {}).get("finance"))}
            for kind, name in briefing.TYPE_OF_KIND.items() if name in briefing.EVENT_TYPES}


def _validate_signal(spec: Mapping[str, Any], held: Iterable[str] | None) -> dict[str, Any]:
    extra = sorted(set(spec) - {"schema", "signal"})
    if extra:
        raise RuleRefused([f"A rule cannot hold '{k}'." for k in extra[:5]])
    if spec.get("schema") != RULE_SCHEMA:
        raise RuleRefused([f"The rule must be a {RULE_SCHEMA} spec."])
    signal = spec.get("signal")
    if not isinstance(signal, Mapping):
        raise RuleRefused(["A signal rule names the findings to watch."])
    extra = sorted(set(signal) - {"kinds", "min_amount", "currency", "priority"})
    if extra:
        raise RuleRefused([f"A signal rule cannot hold '{k}'." for k in extra[:5]])
    known = signal_kinds()
    kinds = signal.get("kinds")
    problems: list[str] = []
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= MAX_SIGNAL_KINDS \
            or not all(isinstance(k, str) for k in kinds):
        raise RuleRefused([f"A signal rule names 1 to {MAX_SIGNAL_KINDS} kinds of finding."])
    for k in kinds:
        if k not in known:
            problems.append(f"'{k[:60]}' is not a kind of finding the checks raise.")
    out: dict[str, Any] = {"kinds": sorted(dict.fromkeys(kinds))}
    if "min_amount" in signal:
        number = _decimal(signal.get("min_amount"))
        if number is None or number < 0:
            problems.append("The amount needs a plain number under a trillion (such as "
                            "50000).")
        else:
            out["min_amount"] = str(number)
    if "currency" in signal:
        currency = signal.get("currency")
        if "min_amount" not in signal or not (isinstance(currency, str)
                                              and _CURRENCY.match(currency)):
            problems.append("A currency is a 3-letter code on an amount only.")
        else:
            out["currency"] = currency
    if "priority" in signal:
        if signal.get("priority") not in PRIORITIES:
            problems.append("A priority is high, medium or low.")
        else:
            out["priority"] = signal["priority"]
    if problems:
        raise RuleRefused(problems)
    norm = {"schema": RULE_SCHEMA, "signal": out}
    if held is not None:
        from ontology import store_ddl
        if signal_markings(norm) and "finance" not in set(store_ddl.with_implied(held)):
            raise RuleRefused(["This rule reads finance figures (" + ", ".join(
                out["kinds"]) + "); only readers cleared for finance may set it."])
    return norm


def signal_markings(spec: Mapping[str, Any]) -> list[str]:
    signal = spec.get("signal") or {}
    known = signal_kinds()
    finance = "min_amount" in signal or any(
        (known.get(k) or {}).get("finance") for k in signal.get("kinds") or ())
    return ["finance"] if finance else []


def spec_markings(spec: Mapping[str, Any], vocab: Vocabulary) -> list[str]:
    if "signal" in spec:
        return signal_markings(spec)
    props = vocab.types.get(spec["subject"]) or {}
    return sorted(set().union(*(props[c["property"]][1] for c in spec["when"]
                                if c["property"] in props)))


def spec_digest(reader: str, spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps([reader, spec], sort_keys=True).encode()).hexdigest()[:32]


def _words(type_id: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", type_id).lower()


def describe(spec: Mapping[str, Any], vocab: Vocabulary | None = None) -> str:
    if "signal" in spec:
        signal, known = spec["signal"], signal_kinds()
        names = [known.get(k, {}).get("label", k) for k in signal["kinds"]]
        text = "Alert me when a check raises a new finding: " + "; ".join(names)
        if "min_amount" in signal:
            text += f", for {signal['min_amount']}" + (
                f" {signal['currency']}" if "currency" in signal else
                " in the finding's own currency") + " or more"
        if signal.get("priority") and signal["priority"] != "low":
            text += f", {signal['priority']} priority" + (
                " only" if signal["priority"] == "high" else " or higher")
        return text + "."
    """The compiled rule in plain words: what the person confirms."""
    noun = _words(spec["subject"])
    parts = []
    for c in spec["when"]:
        prop = c["property"].replace("_", " ")
        text = f"its {prop} {OP_WORDS[c['op']]}"
        if "value" in c:
            value = c["value"]
            if isinstance(value, bool):
                value = "yes" if value else "no"
            elif c["op"] in ("gt", "gte", "lt", "lte") and _decimal(value) is not None \
                    and "-" not in str(value)[1:]:
                value = f"{Decimal(value):,}"
            text += f" {value}"
            if c.get("currency"):
                text += f" {c['currency']} (records in another currency never match)"
            elif vocab is not None and (vocab.types.get(spec["subject"]) or {}).get(
                    c["property"], ("",))[0] == "money":
                text += " in the record's own currency (no conversion)"
        parts.append(text)
    out = f"Alert me when a {noun} changes so that " + " and ".join(parts)
    count = spec.get("count")
    if count:
        group = ""
        if count.get("per"):
            other = (vocab.links.get(count["per"]) if vocab else None) or (None, None)
            group = f" for the same {_words(other[1]) if other[1] else 'linked record'}"
        out += (f", {count['at_least']} times within {count['within_days']} days"
                f"{group}")
    return out + "."


# --------------------------------------------------------------------------- plain words

_NUMBER_WORDS = {"once": 1, "twice": 2, "two": 2, "three": 3, "thrice": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_PERIODS = {"day": 1, "week": 7, "fortnight": 14, "month": 30, "quarter": 91, "year": 365}
_CURRENCY_MARKS = (("us$", "USD"), ("usd", "USD"), ("c$", "CAD"), ("ca$", "CAD"),
                   ("cad", "CAD"), ("eur", "EUR"), ("€", "EUR"), ("gbp", "GBP"), ("£", "GBP"))
# the plain nouns a person uses for record types the schema declares under longer ids
_NOUN_ALIASES = {"order": "SalesOrder", "orders": "SalesOrder", "po": "PurchaseOrder",
                 "pos": "PurchaseOrder", "vendor": "Supplier", "vendors": "Supplier",
                 "item": "Product", "items": "Product"}
_DUE = re.compile(r"(required|expected|due|promised|delivery|ship)")
_SLIP = re.compile(r"\b(slip|slips|slipped|slipping|late|delayed|delay|delays|pushed|"
                   r"moves? later|moved later)\b")


def _noun_types(text: str, vocab: Vocabulary) -> list[tuple[int, str]]:
    """(position, type) of every record-type noun in the text, longest names first."""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    names = sorted(((_words(t), t) for t in vocab.types if not t.startswith("Rel")),
                   key=lambda nt: -len(nt[0]))
    names += [(alias, t) for alias, t in _NOUN_ALIASES.items() if t in vocab.types]
    for name, type_id in names:
        for match in re.finditer(rf"\b{re.escape(name)}s?\b", text):
            span = (match.start(), match.end())
            if any(a < span[1] and span[0] < b for a, b in taken):
                continue
            taken.append(span)
            found.append((span[0], type_id))
    return sorted(found)


def _due_prop(type_id: str, vocab: Vocabulary) -> str | None:
    dates = [p for p, (k, _m) in (vocab.types.get(type_id) or {}).items()
             if k in _DATES and _DUE.search(p) and not _STAMP.search(p)]
    return dates[0] if len(dates) == 1 else (
        next((p for p in dates if p.startswith(("required", "expected", "due"))), None))


def _amount_prop(type_id: str, vocab: Vocabulary) -> str | None:
    """The record's total: its money property named total_*, else one holding 'total',
    else its only amount; several equally good: None (the rule must name it)."""
    money = [p for p, (k, _m) in (vocab.types.get(type_id) or {}).items() if k == "money"]
    for pick in ([p for p in money if p.startswith("total")],
                 [p for p in money if "total" in p], money):
        if len(pick) == 1:
            return pick[0]
        if pick:
            return None
    return None


def compile_text(text: str, vocab: Vocabulary) -> dict[str, Any]:
    """A rule spec from plain words (the deterministic grammar), or ``RuleRefused`` with
    what it could not read. The spec still goes through ``validate``."""
    if not isinstance(text, str) or not text.strip():
        raise RuleRefused(["Write the rule in a sentence."])
    if len(text) > MAX_TEXT:
        raise RuleRefused([f"Keep the rule under {MAX_TEXT} characters."])
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    signal = _signal_text(low)
    if signal is not None:
        return signal
    nouns = _noun_types(low, vocab)
    if not nouns:
        raise RuleRefused(["Name the kind of record the rule is about (an order, a PO, a "
                           "supplier ...)."])
    first = nouns[0][1]
    subject, per = first, None
    when: list[dict[str, Any]] = []
    slip = bool(_SLIP.search(low))
    if slip and _due_prop(first, vocab) is None:
        # "a supplier is late": the records of that party that carry a due date
        hops = [(lid, f) for lid, (f, t) in vocab.links.items()
                if t == first and f in vocab.types and _due_prop(f, vocab)
                and not f.startswith("Rel") and not f.endswith("Line")]
        # a party is late on its orders (an invoice's due date is the party's to meet only
        # when it bills us, so it is never guessed): one order type linked to it, else asked
        orders = [h for h in hops if h[1].endswith("Order")]
        if len(orders) != 1:
            raise RuleRefused([f"'Late' is not one thing for a {_words(first)}: name the "
                               "record that is late (an order, a PO ...)."])
        per, subject = orders[0]
    if slip:
        due = _due_prop(subject, vocab)
        if due is None:
            raise RuleRefused([f"A {_words(subject)} has no due date to slip."])
        when.append({"property": due, "op": "later"})
    amount = re.search(r"\b(over|above|more than|greater than|at least|under|below|less "
                       r"than|at most)\s+([a-z]{0,3}\$|[€£])?\s?(\d[\d,]*(?:\.\d+)?)\s?"
                       r"(k|m|thousand|million)?\b\s?([a-z]{3})?", low)
    if amount:
        prop = _amount_prop(subject, vocab)
        if prop is None:
            raise RuleRefused([f"A {_words(subject)} has no single amount to compare."])
        number = Decimal(amount.group(3).replace(",", ""))
        scale = {"k": 1000, "thousand": 1000, "m": 1_000_000, "million": 1_000_000}
        number *= scale.get(amount.group(4) or "", 1)
        op = {"over": "gt", "above": "gt", "more than": "gt", "greater than": "gt",
              "at least": "gte", "under": "lt", "below": "lt", "less than": "lt",
              "at most": "lte"}[amount.group(1)]
        cond: dict[str, Any] = {"property": prop, "op": op, "value": str(number.normalize()
                                                                        if number % 1 else
                                                                        int(number))}
        marks = ((amount.group(2) or "") + " " + (amount.group(5) or "")).strip()
        for mark, code in _CURRENCY_MARKS:
            if mark in marks.split() or marks.startswith(mark):
                cond["currency"] = code
                break
        when.append(cond)
    becomes = re.search(r"\b(?:status|state)\s+(?:becomes|changes to|goes to|is set to|is)\s+"
                        r"['\"]?([a-z][a-z .-]{0,40}?)['\"]?(?:[,.;]|\s+(?:and|or)\b|\s*$)",
                        low)
    status = "status" if "status" in (vocab.types.get(subject) or {}) else None
    if becomes and status:
        when.append({"property": status, "op": "becomes", "value": becomes.group(1).strip()})
    elif re.search(r"\b(?:status|state)\s+changes?\b", low) and status:
        when.append({"property": status, "op": "changed"})
    if not any(c["op"] in CHANGE_OPS for c in when):
        raise RuleRefused(["Say what change to watch for (slips, goes late, status becomes "
                           "..., status changes)."])
    spec: dict[str, Any] = {"schema": RULE_SCHEMA, "subject": subject, "when": when}
    times = re.search(r"\b(once|twice|thrice|two|three|four|five|six|seven|eight|nine|ten|"
                      r"\d{1,2})\s*(?:times|x)?\s+(?:in|within)\s+(?:a|one|1|the last|"
                      r"(\d{1,3}))?\s*(day|week|fortnight|month|quarter|year)s?\b", low)
    if times:
        least = _NUMBER_WORDS.get(times.group(1))
        least = least if least is not None else int(times.group(1))
        days = _PERIODS[times.group(3)] * (int(times.group(2)) if times.group(2) else 1)
        spec["count"] = {"at_least": least, "within_days": days,
                         **({"per": per} if per else {})}
    return spec


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") \
        else word


def _signal_text(low: str) -> dict[str, Any] | None:
    """A signal spec when the words name finding kinds by their own name or label (the
    longest names first; a word inside a longer matched name is not matched again)."""
    words = " " + " ".join(_stem(w) for w in re.findall(r"[a-z0-9$.,]+", low)) + " "
    names: list[tuple[str, str]] = []
    for kind, info in signal_kinds().items():
        for name in {kind.replace("_", " "), re.sub(r"[^a-z0-9 ]", " ", info["label"].lower())}:
            stems = " ".join(_stem(w) for w in name.split())
            if len(stems.split()) >= 2:
                names.append((stems, kind))
    found: list[str] = []
    for name, kind in sorted(names, key=lambda n: (-len(n[0]), n[1])):
        if f" {name} " in words and kind not in found:
            found.append(kind)
            words = words.replace(f" {name} ", " | ")
    if not found:
        return None
    signal: dict[str, Any] = {"kinds": found[:MAX_SIGNAL_KINDS + 1]}
    amount = re.search(r"\b(over|above|more than|greater than|at least)\s+([a-z]{0,3}\$|[€£])?"
                       r"\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|thousand|million)?\b\s?([a-z]{3})?", low)
    if amount:
        number = Decimal(amount.group(3).replace(",", ""))
        number *= {"k": 1000, "thousand": 1000, "m": 1_000_000,
                   "million": 1_000_000}.get(amount.group(4) or "", 1)
        signal["min_amount"] = str(int(number) if number % 1 == 0 else number.normalize())
        marks = ((amount.group(2) or "") + " " + (amount.group(5) or "")).strip()
        for mark, code in _CURRENCY_MARKS:
            if mark in marks.split() or marks.startswith(mark):
                signal["currency"] = code
                break
    level = re.search(r"\b(high|medium)[- ]priority\b|\b(urgent)\b", low)
    if level:
        signal["priority"] = "high" if level.group(2) else level.group(1)
    return {"schema": RULE_SCHEMA, "signal": signal}


def _moment(text: Any) -> datetime:
    """An ISO moment (``Z`` or an offset; none: UTC); a bad one raises ``ValueError``."""
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def signal_alerts(rules: Iterable[Mapping[str, Any]],
                  findings: Callable[[Sequence[str], str], Sequence[Mapping[str, Any]]],
                  read: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The alerts of a reader's signal rules, read now: ``findings(kinds, since)`` is the
    reader's own inbox read under their grant (``proactive_engine.read_inbox``), newest
    first and only findings first seen at or after ``since`` (an older backlog never uses
    up its bound), so a finding they may not see, or whose figure they may not open, is
    never here. One read serves every rule (the union of their kinds, since the oldest
    rule); each rule keeps its own kinds found since it was made. Fails closed: a read
    that fails shows nothing."""
    read = set(read)
    live = []
    for rule in rules:
        spec = rule.get("spec") or {}
        signal = spec.get("signal") if isinstance(spec, Mapping) else None
        if isinstance(signal, Mapping) and rule.get("state", "active") == "active":
            try:
                live.append((rule, signal, _moment(rule["at"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not live:
        return []
    kinds = sorted({k for _r, signal, _at in live for k in signal.get("kinds") or ()})
    oldest = min(at for _r, _s, at in live)
    try:
        gaps = list(findings(kinds, oldest.isoformat()))
    except Exception:  # noqa: BLE001 - an inbox that cannot be read shows nothing
        return []
    out: list[dict[str, Any]] = []
    for rule, signal, made in live:
        floor = _decimal(signal["min_amount"]) if "min_amount" in signal else None
        lowest = PRIORITIES.index(signal.get("priority") or "low")
        for g in gaps:
            try:
                if g["kind"] not in signal["kinds"] or _moment(g["first_seen"]) < made:
                    continue
                fu = g.get("follow_up") or {}
                if PRIORITIES.index(fu.get("priority") or g.get("priority") or "low") > lowest:
                    continue
                if floor is not None:
                    amount = (g.get("event") or fu.get("event") or {}).get("amount")
                    value = _decimal(str(amount.get("value"))) if isinstance(amount, Mapping) \
                        else None
                    if value is None or abs(value) < floor or (
                            "currency" in signal and amount.get("currency") != signal["currency"]):
                        continue
            except (KeyError, TypeError, ValueError):
                continue
            alert_id = "s_" + hashlib.sha256(f"{rule['rule_id']}|{g['id']}".encode()
                                             ).hexdigest()[:16]
            ref = {"object_id": g["object_id"], "type": g.get("object_type"), "label": None}
            out.append({"alert_id": alert_id, "at": str(g["first_seen"]), "origin": "rule",
                        "kind": "rule", "about": ref, "record": ref, "link_type": None,
                        "changes": [], "rule_id": rule["rule_id"], "rule_text": rule["text"],
                        "counted": None, "read": alert_id in read,
                        "finding": {"kind": g["kind"], "summary": g.get("summary") or ""}})
    out.sort(key=lambda a: (a["at"], a["alert_id"]), reverse=True)
    return out


def compile_rule(text: str, vocab: Vocabulary, held: Iterable[str] | None, *,
                 model: Callable[[str, Mapping[str, Any]], Any] | None = None
                 ) -> dict[str, Any]:
    """{spec, description, markings, compiled_by}: the grammar's spec, else (when the
    grammar cannot read the words and a ``model`` is given) the model's proposal; either is
    ``validate``d, so nothing outside the grammar ever stands."""
    try:
        spec, by = compile_text(text, vocab), "grammar"
    except RuleRefused as refused:
        if model is None:
            raise
        proposal = model(text, {"schema": RULE_SCHEMA, "ops": list(OPS)})
        if not isinstance(proposal, Mapping):
            raise RuleRefused(refused.problems) from None
        spec, by = proposal, "model"
    spec = validate(spec, vocab, held)
    return {"spec": spec, "description": describe(spec, vocab),
            "markings": spec_markings(spec, vocab), "compiled_by": by}


# --------------------------------------------------------------------------- store



def watch_path(store_path: Any) -> Path:
    """The watchlists file of a served store: beside it, as its gap inbox is."""
    store = Path(store_path)
    return store.with_name(store.name + ".watchlists.sqlite")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _schemas.apply(conn, _WATCHLISTS)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (WATCH_SCHEMA,))
    elif row[0] != WATCH_SCHEMA:
        conn.close()
        raise ValueError("watchlists_schema_mismatch")
    return conn


def _now(clock: Callable[[], datetime] | None = None) -> str:
    return (clock or (lambda: datetime.now(timezone.utc)))().isoformat()


class WatchStore:
    """One person's follows and rules (by reader key) in the watchlists file."""

    def __init__(self, path: Any, clock: Callable[[], datetime] | None = None) -> None:
        self.path, self.clock = Path(path), clock

    def follow(self, reader: str, object_id: str, object_type: str | None,
               label: str | None) -> dict[str, Any]:
        if not isinstance(object_id, str) or not _ID.match(object_id):
            raise RuleRefused(["That is not a record id."])
        with closing(_connect(self.path)) as conn:
            have = conn.execute("SELECT count(*) FROM follows WHERE reader = ?",
                                (reader,)).fetchone()[0]
            if have >= MAX_FOLLOWS_PER_READER:
                raise RuleRefused([f"You follow {MAX_FOLLOWS_PER_READER} records already."])
            conn.execute("INSERT OR IGNORE INTO follows (reader, object_id, object_type, "
                         "label, at) VALUES (?, ?, ?, ?, ?)",
                         (reader, object_id, object_type, (label or "")[:200] or None,
                          _now(self.clock)))
        return {"object_id": object_id, "following": True}

    def unfollow(self, reader: str, object_id: str) -> dict[str, Any]:
        with closing(_connect(self.path)) as conn:
            conn.execute("DELETE FROM follows WHERE reader = ? AND object_id = ?",
                         (reader, object_id))
        return {"object_id": object_id, "following": False}

    def follows(self, reader: str) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT object_id, object_type, label, at, baseline_at FROM follows "
                "WHERE reader = ? ORDER BY at DESC", (reader,))]

    def add_rule(self, reader: str, text: str, spec: Mapping[str, Any],
                 description: str) -> dict[str, Any]:
        at = _now(self.clock)
        rule_id = "r_" + hashlib.sha256(f"{reader}|{at}|{json.dumps(spec, sort_keys=True)}"
                                        .encode()).hexdigest()[:16]
        with closing(_connect(self.path)) as conn:
            have = conn.execute("SELECT count(*) FROM rules WHERE reader = ?",
                                (reader,)).fetchone()[0]
            if have >= MAX_RULES_PER_READER:
                raise RuleRefused([f"You have {MAX_RULES_PER_READER} rules already."])
            conn.execute("INSERT INTO rules (rule_id, reader, text, spec, description, at, "
                         "baseline_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (rule_id, reader, text[:MAX_TEXT], json.dumps(spec, sort_keys=True),
                          description, at, at if "signal" in spec else None))
        return {"rule_id": rule_id, "text": text[:MAX_TEXT], "spec": dict(spec),
                "description": description, "at": at, "state": "active"}

    def delete_rule(self, reader: str, rule_id: str) -> bool:
        if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id):
            return False
        with closing(_connect(self.path)) as conn:
            gone = conn.execute("DELETE FROM rules WHERE reader = ? AND rule_id = ?",
                                (reader, rule_id)).rowcount
            conn.execute("DELETE FROM events WHERE rule_id = ?", (rule_id,))
        return bool(gone)

    def rules(self, reader: str) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as conn:
            return [{**dict(r), "spec": json.loads(r["spec"])} for r in conn.execute(
                "SELECT rule_id, text, spec, description, at, baseline_at, state, note "
                "FROM rules WHERE reader = ? ORDER BY at DESC", (reader,))]

    def signal_reads(self, reader: str) -> set[str]:
        with closing(_connect(self.path)) as conn:
            return {r[0] for r in conn.execute(
                "SELECT alert_id FROM signal_reads WHERE reader = ?", (reader,))}

    def mark_read(self, reader: str, alert_ids: Sequence[str]) -> int:
        ids = [a for a in alert_ids if isinstance(a, str) and len(a) <= 80][:500]
        with closing(_connect(self.path)) as conn:
            signals = [a for a in ids if re.fullmatch(r"s_[0-9a-f]{16}", a)]
            marked = sum(conn.execute(
                "INSERT OR IGNORE INTO signal_reads (reader, alert_id, at) VALUES (?, ?, ?)",
                (reader, a, _now(self.clock))).rowcount for a in signals)
            return marked + conn.execute(
                "UPDATE alerts SET read_at = ? WHERE reader = ? AND read_at IS NULL AND "
                "alert_id IN (SELECT value FROM json_each(?))",
                (_now(self.clock), reader, json.dumps(ids))).rowcount

    def raw_alerts(self, reader: str, limit: int) -> list[sqlite3.Row]:
        with closing(_connect(self.path)) as conn:
            return list(conn.execute("SELECT * FROM alerts WHERE reader = ? ORDER BY at DESC, "
                                     "alert_id LIMIT ?", (reader, limit)))


# --------------------------------------------------------------------------- reading

def _chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass
class _Record:
    object_id: str
    object_type: str
    audience: str
    markings: list[str]     # the current versions' markings, union
    handles: list[str]
    props: dict[str, dict[str, Any]]   # prop -> {"v": [values], "m": [markings]}


def _read(st: Any, ids: Iterable[str]) -> dict[str, _Record]:
    """The current records among ``ids`` (removed ones are absent): type, audience,
    versions' markings and handles, every current value."""
    ids = sorted(set(ids))
    out: dict[str, _Record] = {}
    if not ids:
        return out
    meta: dict[str, tuple[str, str]] = {}
    for part in _chunks(ids):
        for oid, kind, aud in st.conn.execute(
                "SELECT object_id, object_type, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
            meta[oid] = (kind, aud)
    versions: dict[str, tuple[set[str], set[str]]] = {}
    for row in st.versions(objects=list(meta)):
        marks, handles = versions.setdefault(row["object_id"], (set(), set()))
        marks.update(json.loads(row["markings"] or "[]"))
        handles.add(row["l1_handle"])
    types = sorted({kind for kind, _ in meta.values()})
    values = st.values(types, objects=list(versions)) if versions else {}
    for oid, (marks, handles) in versions.items():
        kind, aud = meta[oid]
        props: dict[str, dict[str, Any]] = {}
        for prop, rows in (values.get(oid) or {}).get("props", {}).items():
            vals = sorted({_shown(r) for r in rows if r.get("value") is not None})
            vm = sorted(set().union(*(json.loads(r.get("markings") or "[]") for r in rows)))
            props[prop] = {"v": vals, "m": vm}
        out[oid] = _Record(oid, kind, aud, sorted(marks), sorted(handles), props)
    return out


def _shown(row: Mapping[str, Any]) -> str:
    value = row.get("value")
    text = value if isinstance(value, str) else json.dumps(value)
    return f"{text} {row['unit']}" if row.get("unit") else text


def _neighbors(st: Any, ids: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """object -> {neighbor id: [link type, markings]} over current, held links."""
    out: dict[str, dict[str, list[str]]] = {oid: {} for oid in ids}
    if not ids:
        return out
    wanted = set(ids)
    for row in st.touching(list(ids)):
        _rowid, _lid, ltype, a, b, _tier, marks, gone = row
        if gone:
            continue
        if a in wanted:
            out[a].setdefault(b, [ltype, marks])
        if b in wanted:
            out[b].setdefault(a, [ltype, marks])
    return out


def _label(rec: _Record, vocab: Vocabulary) -> str:
    for prop in vocab.label_prop(rec.object_type):
        entry = rec.props.get(prop)
        if entry and entry["v"] and not entry["m"]:
            return entry["v"][0][:120]
    return _words(rec.object_type).capitalize()


def _single(entry: Mapping[str, Any] | None) -> str | None:
    vals = (entry or {}).get("v") or []
    return vals[0] if len(vals) == 1 else None   # several source values: undecided


# --------------------------------------------------------------------------- evaluation

def _cmp_value(kind: str, raw: str | None) -> Any:
    if raw is None:
        return None
    if kind in _DATES:
        text = raw.strip().strip('"')[:10]
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if kind in _NUMBERS:
        return _decimal(raw.split(" ")[0].strip('"'))
    if kind == "boolean":
        low = raw.strip('"').lower()
        return True if low in ("true", "1", "yes") else False if low in (
            "false", "0", "no") else None
    return raw.strip('"').casefold()


def _currency(rec: _Record, prop: str) -> str | None:
    """The currency of an amount: its value's unit, else the record's currency."""
    vals = (rec.props.get(prop) or {}).get("v") or []
    units = {v.split(" ", 1)[1] for v in vals if " " in v}
    if len(units) == 1:
        return units.pop().upper()
    own = _single(rec.props.get("currency"))
    return own.strip('"').upper() if own else None


def judge(spec: Mapping[str, Any], vocab: Vocabulary, old: Mapping[str, Any] | None,
          rec: _Record) -> tuple[bool, list[dict[str, Any]]]:
    """(does the change from ``old`` (the snapshot's props, None: never seen) to ``rec``
    meet every condition, the changes it states)."""
    props = vocab.types[spec["subject"]]
    stated: list[dict[str, Any]] = []
    entering = not any(c["op"] in CHANGE_OPS for c in spec["when"])
    for c in spec["when"]:
        kind, marks = props[c["property"]]
        new_raw = _single(rec.props.get(c["property"]))
        old_raw = _single((old or {}).get(c["property"])) if old is not None else None
        new, before = _cmp_value(kind, new_raw), _cmp_value(kind, old_raw)
        op = c["op"]
        if op in CHANGE_OPS:
            if old is None or c["property"] not in old:
                return False, []    # never seen: no change can be told
            if op == "changed":
                ok = new_raw != old_raw and (new_raw is not None or old_raw is not None)
            elif op == "becomes":
                ok = new == _cmp_value(kind, c["value"]) and before != new
            elif before is None or new is None:
                ok = False
            elif op in ("later", "increased"):
                ok = new > before
            else:
                ok = new < before
        else:
            if new is None:
                return False, []
            target = _cmp_value(kind, c["value"]) if not isinstance(c["value"], bool) \
                else c["value"]
            if c.get("currency") and _currency(rec, c["property"]) != c["currency"]:
                return False, []
            try:
                ok = {"gt": new > target, "gte": new >= target, "lt": new < target,
                      "lte": new <= target, "eq": new == target,
                      "ne": new != target}[op] if target is not None else False
            except TypeError:
                ok = False
        if not ok:
            return False, []
        stated.append({"property": c["property"], "before": old_raw, "after": new_raw,
                       "markings": sorted(set(marks) | set((rec.props.get(c["property"])
                                                            or {}).get("m") or ()))})
    if entering:
        # a rule of states only: told when the record comes to meet it
        if old is not None and all(c["property"] in old for c in spec["when"]):
            was = _Record(rec.object_id, rec.object_type, rec.audience, [], [],
                          {p: dict(v) for p, v in old.items()})
            if judge({**spec}, vocab, None, was)[0]:
                return False, []
    return True, stated


def _diff(old: Mapping[str, Any], rec: _Record, full: bool) -> list[dict[str, Any]]:
    out = []
    names = (set(old) | set(rec.props)) if full else set(old)
    for prop in sorted(names):
        if _STAMP.search(prop):
            continue
        before = (old.get(prop) or {}).get("v") or []
        after = (rec.props.get(prop) or {}).get("v") or []
        if before != after:
            marks = set((old.get(prop) or {}).get("m") or ()) | set(
                (rec.props.get(prop) or {}).get("m") or ())
            out.append({"property": prop, "before": ", ".join(before)[:200] or None,
                        "after": ", ".join(after)[:200] or None, "markings": sorted(marks)})
    return out


def _record_gate(rec: _Record | None, link_marks: str | None = None) -> dict[str, Any] | None:
    if rec is None:
        return None
    marks = set(rec.markings) | set(json.loads(link_marks or "[]"))
    return {"object_id": rec.object_id, "audience": rec.audience, "markings": sorted(marks),
            "handles": rec.handles[:20]}


def _alert_id(*parts: Any) -> str:
    return "w_" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]


def evaluate(st: Any, path: Any, moved: Iterable[str], *,
             clock: Callable[[], datetime] | None = None,
             vocab: Vocabulary | None = None) -> dict[str, Any]:
    """Judge every follow and rule against the records that changed (``moved``, the
    incremental pass's changed records, read in ``st``, a ``proactive_engine._Store``);
    write the alerts and refresh the snapshots. Counts only."""
    started = time.monotonic()
    path = Path(path)
    out: dict[str, Any] = {"follows": 0, "rules": 0, "alerts": 0}
    if not path.is_file():
        return {**out, "state": "none"}   # nobody follows anything yet
    vocab = vocab or vocabulary()
    at = _now(clock)
    moment = datetime.fromisoformat(at)
    moved = set(moved)
    with closing(_connect(path)) as conn:
        follows = [dict(r) for r in conn.execute("SELECT * FROM follows")]
        rules = [dict(r) for r in conn.execute("SELECT * FROM rules WHERE state = 'active'")
                 if "signal" not in json.loads(r["spec"])]   # signal rules: read when looked at
        out.update(follows=len(follows), rules=len(rules))
        if not follows and not rules:
            return {**out, "state": "idle"}
        alerts: list[tuple] = []
        snaps = _Snapshots(conn, at)

        # ---- baselines: what a new follow or rule finds is what later changes are told from
        fresh_follows = sorted({f["object_id"] for f in follows if f["baseline_at"] is None})
        if fresh_follows:
            fnb = _neighbors(st, fresh_follows)
            recs = _read(st, set(fresh_follows) | {n for oid in fresh_follows
                                                   for n in _capped(fnb[oid])})
            for oid in fresh_follows:
                if oid in recs:
                    snaps.put(recs[oid], True, neighbors=_capped(fnb[oid]))
                for n in _capped(fnb[oid]):
                    if n in recs:
                        snaps.put(recs[n], True)
            conn.execute("UPDATE follows SET baseline_at = ? WHERE baseline_at IS NULL AND "
                         "object_id IN (SELECT value FROM json_each(?))",
                         (at, json.dumps(fresh_follows)))
        # a new rule's first look reads its subject type a page at a time, within
        # ``BASELINE_SECONDS`` a pass (``cursor``: the last record read), so the incremental
        # pass is never held up; the rule judges once the look is whole. A record of the
        # type that moves meanwhile, already looked at, is looked at again (below).
        fresh_rules = [r for r in rules if r["baseline_at"] is None]
        budget_end = time.monotonic() + BASELINE_SECONDS
        first = True   # one page a pass at least, so a look always ends
        for rule in fresh_rules:
            spec = json.loads(rule["spec"])
            props = _rule_props(spec)
            if rule["cursor"] is None and st._type_count(spec["subject"]) > RULE_BASELINE_CAP:
                conn.execute("UPDATE rules SET state = 'refused', note = ? WHERE rule_id = ?",
                             (f"{spec['subject']} has too many records to watch",
                              rule["rule_id"]))
                continue
            cursor = rule["cursor"] or ""
            while first or time.monotonic() < budget_end:
                first = False
                ids = [r[0] for r in st.conn.execute(
                    "SELECT object_id FROM objects WHERE object_type = ? AND object_id > ? "
                    "ORDER BY object_id LIMIT ?", (spec["subject"], cursor, BASELINE_PAGE))]
                if ids:
                    values = st.values([spec["subject"]], props, objects=ids)
                    prior = snaps.kept(values)
                    for oid, entry in values.items():
                        if (prior.get(oid) or {}).get("full"):
                            continue   # a followed record's whole snapshot holds these
                        rec = _Record(oid, spec["subject"], "", [], [],
                                      _props(entry["props"]))
                        snaps.put(rec, False, props, prior=prior.get(oid), keep_gate=True)
                    cursor = ids[-1]
                done = len(ids) < BASELINE_PAGE
                conn.execute("UPDATE rules SET cursor = ?, baseline_at = ? WHERE rule_id = ?",
                             (cursor, at if done else None, rule["rule_id"]))
                snaps.flush()
                if done:
                    rule["baseline_at"] = at
                    break
            rule["cursor"] = cursor
        looking = [r for r in fresh_rules if r["baseline_at"] is None and r["cursor"]]
        if looking and moved:
            # what moved since it was looked at: looked at again, never judged yet
            kinds = _types(st, moved)
            for rule in looking:
                spec = json.loads(rule["spec"])
                again = sorted(m for m in moved if kinds.get(m) == spec["subject"]
                               and m <= rule["cursor"])
                if again:
                    prior = snaps.kept(again)
                    for oid, rec in _read(st, again).items():
                        if not (prior.get(oid) or {}).get("full"):
                            snaps.put(rec, False, _rule_props(spec), prior=prior.get(oid),
                                      keep_gate=True)
        out["rules_looking"] = sum(r["baseline_at"] is None for r in fresh_rules)
        snaps.flush()
        out["baselined"] = len(fresh_follows) + sum(r["baseline_at"] is not None
                                                    for r in fresh_rules)
        follows = [f for f in follows if f["baseline_at"] is not None]
        rules = [r for r in rules if r["baseline_at"] is not None]
        if not moved or (not follows and not rules):
            return {**out, "state": "ran", "changed": len(moved),
                    "seconds": round(time.monotonic() - started, 3)}

        # ---- what moved: its links now, the snapshots of it and of what is followed
        previous = snaps.kept(moved)
        followed: dict[str, list[dict[str, Any]]] = {}
        for f in follows:
            followed.setdefault(f["object_id"], []).append(f)
        types = _types(st, moved)
        rule_types = {json.loads(r["spec"])["subject"] for r in rules}
        # the links of what moved and matters: a followed record, one kept (linked to a
        # followed one), a rule's subject (its count's group); a record that moved only
        # by a new link moves the followed end too (the fingerprint holds its links)
        nbs = _neighbors(st, sorted(m for m in moved if m in followed or m in previous
                                    or types.get(m) in rule_types))
        f_prior = snaps.kept(followed)
        hits: dict[str, set[str]] = {}   # followed id -> moved records about it
        for m in moved:
            if m in followed:
                hits.setdefault(m, set()).add(m)
            for n in set(nbs.get(m) or ()) | set((previous.get(m) or {}).get("neighbors")
                                                  or ()):
                if n in followed:
                    hits.setdefault(n, set()).add(m)
        links_now = {fid: (nbs.get(fid) or {}) if fid in moved
                     else ((f_prior.get(fid) or {}).get("neighbors") or {}) for fid in hits}
        wanted = set(hits) | set().union(set(), *hits.values()) | {
            n for fid in hits for n in set(links_now[fid]) - set(
                (f_prior.get(fid) or {}).get("neighbors") or {})} | {
            m for m in moved if types.get(m) in rule_types}
        recs = _read(st, wanted)

        # ---- follows
        for fid, about in sorted(hits.items()):
            frec, fprior = recs.get(fid), f_prior.get(fid) or {}
            old_nbs, fnbs = fprior.get("neighbors") or {}, links_now[fid]
            events: list[tuple[str, str, list[dict[str, Any]], Any]] = []
            if fid in moved and fprior:
                if frec is None:
                    events.append(("removed", fid, [], None))
                else:
                    changes = _diff(fprior.get("props") or {}, frec, fprior.get("full", True))
                    if changes:
                        events.append(("changed", fid, changes, None))
            if fid in moved:
                for n in sorted(set(fnbs) - set(old_nbs)):
                    events.append(("linked", n, [], fnbs[n]))
                for n in sorted(set(old_nbs) - set(fnbs)):
                    events.append(("unlinked", n, [], old_nbs[n]))
            for m in sorted(about - {fid}):
                rec = recs.get(m)
                if m in fnbs and m in old_nbs and m in previous and rec is not None:
                    changes = _diff(previous[m]["props"], rec, previous[m]["full"])
                    if changes:
                        events.append(("linked_changed", m, changes, fnbs[m]))
            if frec is not None:
                snaps.put(frec, True, neighbors=_capped(fnbs))
            fgate = _record_gate(frec) if frec is not None else fprior.get("gate")
            for kind, oid, changes, link in events:
                rec = recs.get(oid)
                if oid != fid and rec is not None:
                    snaps.put(rec, True, prior=previous.get(oid))
                if oid == fid:
                    gates = [fgate]
                else:
                    other = _record_gate(rec, link[1] if link else None) if rec is not None else \
                        (previous.get(oid) or snaps.kept([oid]).get(oid) or {}).get("gate")
                    if other is None and kind != "unlinked":
                        continue
                    gates = [fgate, other or _UNKNOWN(oid)]
                body = {"about": {"object_id": fid,
                                  "type": frec.object_type if frec else fprior.get("type"),
                                  "label": _label(frec, vocab) if frec else
                                  fprior.get("label")},
                        "record": {"object_id": oid,
                                   "type": rec.object_type if rec else
                                   (previous.get(oid) or {}).get("type"),
                                   "label": _label(rec, vocab) if rec else None},
                        "link_type": link[0] if link else None, "changes": changes}
                for owner in followed[fid]:
                    alerts.append((_alert_id(owner["reader"], "follow", fid, kind, oid,
                                             changes, at),
                                   owner["reader"], "follow", fid, kind, fid, oid,
                                   json.dumps(body, sort_keys=True),
                                   json.dumps([g or _UNKNOWN(fid) for g in gates]), at))

        # ---- rules
        for rule in rules:
            spec = json.loads(rule["spec"])
            props, count = _rule_props(spec), spec.get("count")
            matched: dict[str, list[tuple[str, list]]] = {}
            for oid in sorted(moved):
                rec = recs.get(oid)
                if rec is None or rec.object_type != spec["subject"]:
                    continue
                old = previous.get(oid)
                try:
                    ok, stated = judge(spec, vocab, old["props"] if old else None, rec)
                except (ArithmeticError, ValueError, TypeError):
                    ok, stated = False, []   # a value no comparison can read: skipped
                    out["values_skipped"] = out.get("values_skipped", 0) + 1
                if not (old or {}).get("full"):
                    snaps.put(rec, False, props, prior=old)
                if not ok:
                    continue
                if not count:
                    if not _told(conn, rule["rule_id"], oid, stated):
                        _rule_alert(alerts, rule, spec, rec, None, stated, [oid], oid,
                                    [_record_gate(rec)], at, vocab)
                    continue
                per = count.get("per")
                for grp in (sorted(n for n, (lt, _mk) in (nbs.get(oid) or {}).items()
                                   if lt == per) if per else [oid]):
                    conn.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                                 (rule["rule_id"], grp, oid, at))
                    matched.setdefault(grp, []).append((oid, stated))
            # each group judged once, over every change of this pass counted
            since = (moment - timedelta(days=count["within_days"])).isoformat() \
                if count else at
            for grp, found in sorted(matched.items()):
                rows = list(dict.fromkeys(r[0] for r in conn.execute(
                    "SELECT subject FROM events WHERE rule_id = ? AND grp = ? AND at > ? "
                    "ORDER BY at", (rule["rule_id"], grp, since))))
                if len(rows) < count["at_least"] or conn.execute(
                        "SELECT 1 FROM alerts WHERE origin = 'rule' AND origin_id = ? AND "
                        "about = ? AND at > ?", (rule["rule_id"], grp, since)).fetchone():
                    continue   # not yet, or told already within this window
                others = _read(st, set(rows) | {grp})
                if not all(s in others for s in rows):
                    continue   # a counted record now gone: not told
                oid, stated = found[-1]
                gates = [_record_gate(others[s]) for s in rows]
                if grp != oid:
                    gates.append(_record_gate(others[grp], nbs[oid][grp][1])
                                 if grp in others else _UNKNOWN(grp))
                _rule_alert(alerts, rule, spec, others[oid], others.get(grp), stated, rows,
                            grp, gates, at, vocab)
        # every other kept snapshot of a moved record (a followed record's linked ones)
        for m in moved:
            if m in previous and m in recs and not snaps.has(m):
                snaps.put(recs[m], previous[m]["full"], prior=previous[m])
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT OR IGNORE INTO alerts (alert_id, reader, origin, origin_id, "
                         "kind, about, object_id, body, gates, at) VALUES (?, ?, ?, ?, ?, ?, "
                         "?, ?, ?, ?)", alerts)
        snaps.flush(begun=True)
        conn.execute("COMMIT")
        out.update(state="ran", changed=len(moved), alerts=len(alerts),
                   seconds=round(time.monotonic() - started, 3))
        return out


def _UNKNOWN(oid: str) -> dict[str, Any]:
    """The gate of a record whose audience is not known: no reader is told of it."""
    return {"object_id": oid, "audience": None, "markings": [], "handles": []}


def _capped(neighbors: Mapping[str, Any]) -> dict[str, Any]:
    return dict(sorted(neighbors.items())[:NEIGHBOR_CAP])


def _rule_props(spec: Mapping[str, Any]) -> list[str]:
    return sorted({c["property"] for c in spec["when"]} | {"currency"})


def _props(rows_by_prop: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict]:
    return {p: {"v": sorted({_shown(r) for r in rows if r.get("value") is not None}),
                "m": sorted(set().union(*(json.loads(r.get("markings") or "[]")
                                          for r in rows)))}
            for p, rows in rows_by_prop.items()}


def _types(st: Any, ids: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _chunks(sorted(set(ids))):
        for oid, kind in st.conn.execute(
                "SELECT object_id, object_type FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
            out[oid] = kind
    return out


class _Snapshots:
    """The kept snapshots (``seen``): read by id, written once per pass."""

    def __init__(self, conn: sqlite3.Connection, at: str) -> None:
        self.conn, self.at, self.rows = conn, at, {}

    def kept(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        got: dict[str, dict[str, Any]] = {}
        for part in _chunks(sorted(set(ids))):
            for r in self.conn.execute("SELECT * FROM seen WHERE object_id IN (SELECT value "
                                       "FROM json_each(?))", (json.dumps(list(part)),)):
                got[r["object_id"]] = {"full": bool(r["full"]), "type": r["object_type"],
                                       "props": json.loads(r["props"]),
                                       "neighbors": json.loads(r["neighbors"] or "null"),
                                       "gate": json.loads(r["gate"] or "null"),
                                       "label": r["label"]}
        return got

    def has(self, oid: str) -> bool:
        return oid in self.rows

    def put(self, rec: _Record, full: bool, tracked: Iterable[str] = (), *,
            neighbors: Mapping[str, Any] | None = None, prior: Mapping[str, Any] | None = None,
            keep_gate: bool = False) -> None:
        if full:
            props = rec.props
        else:
            names = set(tracked) | set((prior or {}).get("props") or ())
            props = {p: rec.props.get(p) or {"v": [], "m": []} for p in names}
        nb = neighbors if neighbors is not None else (prior or {}).get("neighbors")
        gate = (prior or {}).get("gate") if keep_gate else _record_gate(rec)
        vocab = vocabulary() if full else None
        self.rows[rec.object_id] = (
            rec.object_id, rec.object_type, int(full), json.dumps(props, sort_keys=True),
            json.dumps(nb, sort_keys=True) if nb is not None else None,
            json.dumps(gate) if gate else None,
            _label(rec, vocab) if vocab and rec.object_type in vocab.types else None, self.at)

    def flush(self, *, begun: bool = False) -> None:
        if not self.rows:
            return
        if not begun:
            self.conn.execute("BEGIN IMMEDIATE")
        self.conn.executemany("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              list(self.rows.values()))
        if not begun:
            self.conn.execute("COMMIT")
        self.rows = {}


def _told(conn: sqlite3.Connection, rule_id: str, oid: str,
          stated: Sequence[Mapping[str, Any]]) -> bool:
    """True when this very change of this record was told already (a pass replayed)."""
    after = json.dumps([[s["property"], s["after"]] for s in stated])
    for r in conn.execute("SELECT body FROM alerts WHERE origin = 'rule' AND origin_id = ? AND "
                          "object_id = ? ORDER BY at DESC LIMIT 1", (rule_id, oid)):
        body = json.loads(r[0])
        return json.dumps([[s["property"], s["after"]] for s in body.get("changes") or ()]) \
            == after
    return False


def _rule_alert(alerts: list, rule: Mapping[str, Any], spec: Mapping[str, Any], rec: _Record,
                grp: _Record | None, stated: list, counted: list, group: str, gates: list,
                at: str, vocab: Vocabulary) -> None:
    body = {"about": {"object_id": group, "type": grp.object_type if grp else rec.object_type,
                      "label": _label(grp, vocab) if grp else _label(rec, vocab)},
            "record": {"object_id": rec.object_id, "type": rec.object_type,
                       "label": _label(rec, vocab)},
            "rule": {"rule_id": rule["rule_id"], "text": rule["text"],
                     "description": rule["description"],
                     "markings": spec_markings(spec, vocab)},
            "counted": counted, "changes": stated}
    alerts.append((_alert_id(rule["reader"], "rule", rule["rule_id"], rec.object_id, group,
                             stated, at),
                   rule["reader"], "rule", rule["rule_id"], "rule", group, rec.object_id,
                   json.dumps(body, sort_keys=True), json.dumps([g for g in gates if g]), at))


# --------------------------------------------------------------------------- showing

def trim(rows: Iterable[Mapping[str, Any]], may_read: Callable[[str], bool],
         held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
         current: Callable[[Iterable[str]], Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The alerts this reader may see now, each stating only values whose markings it
    holds. Each record an alert rests on is checked as it stands now (``current``: id ->
    ``_Record`` of the served store): its current audience, its current markings with
    those kept (a link's), its current handles with those kept; a record no longer held
    keeps the gate it had (it cannot have been narrowed since). Fails closed: a check
    that fails hides the alert."""
    from ontology import store_ddl
    held = set(store_ddl.with_implied(held))
    rows = list(rows)
    try:
        stored = {r["alert_id"]: json.loads(r["gates"]) for r in rows}
        now = current(sorted({g["object_id"] for gates in stored.values() for g in gates}))
    except Exception:  # noqa: BLE001 - the records cannot be read now: nothing is shown
        return []
    for gates in stored.values():
        for g in gates:
            rec = now.get(g["object_id"])
            if rec is not None:
                g["audience"] = rec.audience
                g["markings"] = sorted(set(g["markings"]) | set(rec.markings))
                g["handles"] = sorted(set(g["handles"]) | set(rec.handles))
    handles = sorted({h for gates in stored.values() for g in gates for h in g["handles"]})
    try:
        refused = {h for h, why in (decide(handles) if handles else {}).items()
                   if why is not None}
    except Exception:  # noqa: BLE001 - an undecidable item is not shown
        return []
    out = []
    for row in rows:
        try:
            gates = stored[row["alert_id"]]
            body = json.loads(row["body"])
            if not gates:
                continue
            if any(g["audience"] is None or may_read(g["audience"]) is not True
                   or not set(g["markings"]) <= held or set(g["handles"]) & refused
                   for g in gates):
                continue
            rule = body.get("rule") or {}
            if not set(rule.get("markings") or ()) <= held:
                continue   # a rule over figures this reader no longer holds
            changes = [{k: c[k] for k in ("property", "before", "after")}
                       for c in body.get("changes") or () if set(c.get("markings") or ())
                       <= held]
            if body.get("changes") and not changes:
                continue
        except Exception:  # noqa: BLE001 - an unreadable alert is not shown
            continue
        out.append({"alert_id": row["alert_id"], "at": row["at"], "origin": row["origin"],
                    "kind": row["kind"], "about": body["about"], "record": body["record"],
                    "link_type": body.get("link_type"), "changes": changes,
                    "rule_id": rule.get("rule_id"), "rule_text": rule.get("text"),
                    "counted": len(body.get("counted") or ()) or None,
                    "read": row["read_at"] is not None})
    return out


def sentence(alert: Mapping[str, Any]) -> str:
    """One plain line for an alert as shown."""
    if alert.get("finding"):
        return alert["finding"]["summary"].rstrip(".") + "."
    about = alert["about"].get("label") or alert["about"]["object_id"]
    record = alert["record"].get("label") or alert["record"]["object_id"]
    noun = _words(alert["record"].get("type") or "record")
    if alert["kind"] == "changed":
        what = "; ".join(f"{c['property'].replace('_', ' ')} {c['before'] or '(none)'} -> "
                         f"{c['after'] or '(none)'}" for c in alert["changes"])
        return f"{about} changed: {what}."
    if alert["kind"] == "linked":
        return f"New {noun} linked to {about}: {record}."
    if alert["kind"] == "unlinked":
        return f"{record} is no longer linked to {about}."
    if alert["kind"] == "removed":
        return f"{about} was removed from its source."
    if alert["kind"] == "linked_changed":
        what = "; ".join(f"{c['property'].replace('_', ' ')} {c['before'] or '(none)'} -> "
                         f"{c['after'] or '(none)'}" for c in alert["changes"])
        return f"{noun.capitalize()} {record} linked to {about} changed: {what}."
    times = f" ({alert['counted']} times)" if alert.get("counted", 0) and \
        alert["counted"] > 1 else ""
    return f"Your rule \"{alert.get('rule_text')}\" matched {record}{times}."
