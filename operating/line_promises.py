"""A supplier confirmation's per-item promise dates against the order's own lines.

A confirmation may promise each item its own date (``SupplierConfirmation.line_promises``:
one block per item of its line table, ``ontology/document_fields.py``). Read on its own a
document promising 01/17 for one item and 01/31 for another has no one date
(``conflicting_values``); per line each order line is judged against the date promised
for that line, and the order is late when any open line is past its own promised arrival.

Matching (:func:`match`) is firm evidence only, never position or similarity:

* **item code**: the order line's own item number (``item_code``; a BC line only when it
  is an ``Item`` line) equals one code the block's line writes (or a labelled customer
  material number), compared case-folded with spaces dropped;
* **description**: the order line's description, case-folded with marks (``®``, ``™``) and
  punctuation dropped, is a whole-word run of the block's text; a description of fewer
  than :data:`MIN_DESCRIPTION_CHARS` letters and digits, or of one word without a digit,
  is too generic to match.

A line whose code and description point at different blocks, or at several, is not
matched; a block two lines point at matches neither (both ways unique). The supplier's
own line numbers (10, 20, ...) are its numbering, never ours: no order line states the
number the supplier gives it, so a line number alone never matches. A block no line
matches keeps its date at document level, flagged ``unmatched`` (stated, never judged
against a line). When no block matches at all, the caller keeps the document-level
verdict.

Lines are judged (:func:`judge`) as the document-level verdict judges the order
(``po_status.vendor_verdict``): an open line (nothing or part received, or its state
unread) against today, late when today is after its promise. A received line has no
receipt date of its own here: with the whole order received it is on time when the last
receipt is by its promise, else undetermined. The order is ``late`` when any open line
is late (the furthest past named), or, received in full with every line matched, when
the last receipt is after every line's promise (the line received last was late);
``not_due`` (open) or ``on_time`` (received) only when every line it reads is matched and
none is late, and the order's lines are proven complete for this reader; anything else is ``undetermined`` with the reason
``line_promises_partial``. Stdlib only.
"""

from __future__ import annotations

from datetime import date
import json
import re
from typing import Any, Callable, Mapping, Sequence

MIN_DESCRIPTION_CHARS = 6
MAX_LISTED = 5          # judged lines listed in the verdict (late first)
MAX_UNMATCHED = 3       # unmatched promise dates listed
_MARKS = re.compile(r"[®™©]")
_NON_WORD = re.compile(r"[^0-9a-z]+")
OPEN_STATES = ("not_received", "partial", "unknown")


def fold_text(text: Any) -> str:
    """Lower case, marks dropped, every run of other characters one space."""
    if not isinstance(text, str):
        return ""
    return _NON_WORD.sub(" ", _MARKS.sub("", text).casefold()).strip()


def fold_code(code: Any) -> str:
    return re.sub(r"\s+", "", code.casefold()) if isinstance(code, str) else ""


def _firm_code(code: str) -> bool:
    return len(code) >= 4 and any(c.isdigit() for c in code)


def _firm_description(folded: str) -> bool:
    words = folded.split()
    return (len(folded.replace(" ", "")) >= MIN_DESCRIPTION_CHARS
            and (len(words) >= 2 or any(c.isdigit() for c in folded)))


def descriptor(read: Callable[[str], Sequence[str]]) -> dict[str, list[str]]:
    """One order line's own item codes and descriptions for :func:`match`, from
    ``read(property)`` (the values this reader may read of that line): a line's item
    number counts only when the line is an ``Item`` line, or states no line type."""
    kinds = {str(k).casefold() for k in read("line_type")}
    codes = list(read("item_code")) if not kinds or kinds == {"item"} else []
    return {"codes": codes, "descriptions": list(read("description"))}


def blocks_of(raw: Any) -> list[dict[str, Any]]:
    """The item blocks of a stored ``line_promises`` value (a JSON list or its text);
    anything else is none."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [b for b in raw if isinstance(b, Mapping) and isinstance(b.get("text"), str)]


def match(lines: Mapping[str, Mapping[str, Sequence[str]]],
          blocks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, tuple[int, str]], list[int]]:
    """``lines``: order line id -> ``{"codes": [...], "descriptions": [...]}`` (its own
    values); ``blocks``: a confirmation's item blocks. Returns (line id -> (block index,
    ``item_code`` or ``description``), indexes of the blocks no line matched)."""
    block_codes = [{fold_code(c) for c in b.get("codes") or () if isinstance(c, str)}
                   for b in blocks]
    block_text = [f" {fold_text(b.get('text'))} " for b in blocks]
    chosen: dict[str, tuple[int, str]] = {}
    for lid, own in lines.items():
        codes = {fold_code(c) for c in own.get("codes", ())} - {""}
        by_code = {i for i, found in enumerate(block_codes)
                   if any(_firm_code(c) and c in found for c in codes)}
        texts = {fold_text(d) for d in own.get("descriptions", ())} - {""}
        by_text = {i for i, text in enumerate(block_text)
                   if any(_firm_description(t) and f" {t} " in text for t in texts)}
        if by_code and by_text and not by_code & by_text:
            continue   # code and description point at different items: no evidence
        found, basis = (by_code & by_text or by_code, "item_code") if by_code \
            else (by_text, "description")
        if len(found) == 1:
            chosen[lid] = (next(iter(found)), basis)
    taken: dict[int, list[str]] = {}
    for lid, (index, _) in chosen.items():
        taken.setdefault(index, []).append(lid)
    out = {lid: hit for lid, hit in chosen.items() if len(taken[hit[0]]) == 1}
    used = {index for index, _ in out.values()}
    return out, [i for i in range(len(blocks)) if i not in used]


def judge(lines: Sequence[Mapping[str, Any]], promises: Mapping[str, Mapping[str, Any]],
          measured: date | None, against: str, lines_proven: bool,
          coverage_unknown: bool) -> dict[str, Any]:
    """The per-line verdict and its roll-up. ``lines``: the order's lines as read
    (``line``, ``state``); ``promises``: line id -> ``{"promise": po_status.Promise,
    "basis": ..., "values": [...]}`` for the matched lines; ``measured``: today (open)
    or the last receipt (fully received), ``against`` saying which."""
    judged: list[dict[str, Any]] = []
    unmatched_open = 0
    for line in lines:
        lid, open_line = line["line"], line["state"] in OPEN_STATES
        found = promises.get(lid)
        if found is None:
            unmatched_open += open_line or against == "received"
            continue
        promise = found["promise"]
        out: dict[str, Any] = {"line": lid, "matched_by": found["basis"]}
        if promise.latest is None:
            out.update(state="undetermined", late=None, reason="promises_disagree")
        else:
            day = promise.latest
            out["promised_date"] = day.isoformat()
            if measured is None:
                out.update(state="undetermined", late=None)
            elif against == "today" and not open_line:
                out.update(state="received", late=None)   # its own receipt date is unread
            elif against == "received":
                out.update(state="on_time", late=False) if measured <= day else \
                    out.update(state="undetermined", late=None)
            elif measured > day:
                out.update(state="late", late=True, days_late=(measured - day).days)
                if coverage_unknown:
                    out.update(state="undetermined", late=None)
            else:
                out.update(state="not_due", late=False)
        if found.get("values"):
            out["values"] = list(found["values"])[-1:]
        judged.append(out)
    late = [j for j in judged if j["state"] == "late"]
    complete = lines_proven and not unmatched_open and bool(judged)
    days = [date.fromisoformat(j["promised_date"]) for j in judged if "promised_date" in j]
    roll: dict[str, Any]
    if (not late and against == "received" and complete and measured is not None
            and len(days) == len(judged) and measured > max(days)):
        # the line received last came after every line's promise: that line was late
        roll = {"state": "late", "late": True, "promised_date": max(days).isoformat(),
                "days_late": (measured - max(days)).days}
    elif late:
        worst = max(late, key=lambda j: j["days_late"])
        roll = {"state": "late", "late": True, "promised_date": worst["promised_date"],
                "days_late": worst["days_late"]}
    elif (complete and all(j["state"] in ("not_due", "on_time", "received") for j in judged)
          and any(j["state"] != "received" for j in judged)):
        state = "on_time" if against == "received" else "not_due"
        dated = [j for j in judged if j["state"] == state]
        roll = {"state": state, "late": False,
                "promised_date": min(j["promised_date"] for j in dated)}
    else:
        roll = {"state": "undetermined", "late": None, "reason": "line_promises_partial"}
    order = {"late": 0, "undetermined": 1, "not_due": 2, "on_time": 3, "received": 4}
    # late lines furthest past first; not-due lines latest promise first (the ones a
    # reader of a capped list, such as the impact chain, most needs to see)
    judged.sort(key=lambda j: (order[j["state"]], -j.get("days_late", 0),
                               -date.fromisoformat(j["promised_date"]).toordinal()
                               if j["state"] == "not_due" else 0, j["line"]))
    roll["lines"] = judged[:MAX_LISTED]
    roll["lines_judged"] = len(judged)
    roll["lines_unmatched"] = unmatched_open
    return roll
