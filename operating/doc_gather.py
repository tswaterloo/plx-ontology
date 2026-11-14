"""Facts for a document from across what the reader may read, not from one record.

A form or a quote asks for things no single record holds: a sample's name, the prices a
colleague quoted in mail, a contact's title. ``gather`` reads, through the reader's own
tools (``call``: ``plx.find``, ``plx.object``/``plx.around`` through
``typed_graph.GraphRepository``, ``plx.documents``, ``plx.read``), so the read policy
applies there and nothing here widens it:

* **Records**: every record ``plx.find`` matches the request's words closely (``exact`` or
  ``strong``, at most ``MAX_RECORDS``; a looser ``lead`` only when it is the one record
  found), each with its declared neighbours
  (``doc_fill.facts_from``). Records that disagree on a value leave it ambiguous.
* **Documents**: the mail, attachments and files ``plx.documents`` finds for those words
  (at most ``MAX_DOCUMENTS``, none dated after ``before`` when it is given), each read
  with ``plx.read``. Their text gives ``Label: value`` lines (``text_facts``: key
  ``Text.<label>``) and quoted prices with the quantity each is for (``quoted_prices``:
  key ``Quoted.price@<quantity> <unit>``, the line's leading words as the item).

Every fact cites the record or document it was read from. Nothing here writes.
Evidence label of tests: ``isolated-fixture``; a run on the served store is live-unverified.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import re
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import doc_fill as df
from operating.doc_style import values_in

MAX_RECORDS = 3
MAX_DOCUMENTS = 6
DOCUMENT_TYPES = frozenset({"Email", "Attachment", "ContainerMember", "SharePointFile",
                            "TeamsMessage", "Meeting"})
MAX_TEXT = 60_000
MAX_TEXT_FACTS = 200
# the first note of a gather the time limit cut short: what was read is used, labelled
PARTIAL = "Partial: the time limit was reached; only what was read by then is used"
_LABELLED = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /#&().'-]{1,40}?)\s*:\s*(\S.{0,160}?)\s*$")


def text_facts(text: str, handle: str) -> list[df.Fact]:
    """``Label: value`` lines of a document's text, each cited to the document."""
    out = []
    for line in str(text or "").splitlines():
        m = _LABELLED.match(line)
        if not m or "http" in m.group(1).lower():
            continue
        label = df.normalize(m.group(1))
        if not label or len(out) >= MAX_TEXT_FACTS:
            continue
        out.append(df.Fact(f"Text.{label}", " ".join(m.group(2).split()),
                           df.Citation(handle, m.group(1).strip()[:80], handle)))
    return out


def quoted_prices(text: str, handle: str) -> list[df.Fact]:
    """Prices a document quotes with the quantity each is for: a line holding quantities
    and as many prices (paired in order), or one quantity and one price. The value is the
    amount with its currency code when the line names one."""
    out = []
    for line in str(text or "").splitlines():
        found = values_in(line)
        qty = [v for v in found if v.type == "qty" and v.number is not None]
        money = [v for v in found if v.type == "money" and v.number is not None]
        if not qty or not money or (len(qty) != len(money) and not (
                len(qty) == 1 and len(money) == 1)):
            continue
        first = min(v.start for v in found)
        item = " ".join(line[:first].split()).strip(" :-–|")[:80]
        for q, p in zip(qty, money):
            amount = format(p.number, "f")
            value = f"{amount} {p.code}" if p.code else amount
            out.append(df.Fact(f"Quoted.price@{_number(q.number)} {q.unit}", value,
                               df.Citation(handle, item or "price", handle)))
    return out


def _number(n: Optional[Decimal]) -> str:
    text = format(n, "f") if n is not None else ""
    return text.rstrip("0").rstrip(".") if "." in text else text


def tier_key(quantity: Any, unit: str) -> str:
    """The fact key of a quoted price for that tier."""
    return f"Quoted.price@{_number(Decimal(str(quantity)))} {unit}"


_SKIP_KEYS = frozenset({"citation", "handle", "digest", "lineage", "meta", "gaps",
                        "completeness", "status", "source", "l1_handle", "open_url"})


def _strings(value: Any, out: list[str], room: list[int]) -> None:
    """Every text leaf of a read (its content and field values), in order, within room."""
    if room[0] <= 0:
        return
    if isinstance(value, str):
        out.append(value[:room[0]])
        room[0] -= len(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key not in _SKIP_KEYS:
                _strings(item, out, room)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out, room)


def _text_of(read: Mapping[str, Any]) -> str:
    """A ``plx.read`` result's text: its content first, then its fields' values."""
    out: list[str] = []
    room = [MAX_TEXT]
    _strings(read.get("content"), out, room)
    _strings(read.get("fields"), out, room)
    return "\n".join(s for s in out if len(s) > 1)


def _day(doc: Mapping[str, Any]) -> str:
    for key in ("sent_at", "at", "date", "last_modified", "received_at"):
        value = doc.get(key)
        if isinstance(value, str) and value[:4].isdigit():
            return value[:10]
    return ""


class _Late(Exception):
    """The deadline passed before the next read."""


def gather(call: Callable[[str, dict[str, Any]], Any], words: str, terms: df.Terms, *,
           repo: Any = None, before: Optional[str] = None, deadline: Optional[float] = None
           ) -> tuple[list[df.Fact], list[str]]:
    """The facts for a document about ``words`` (module doc) and notes on what was not
    read. ``call`` raises on a refused or failed read; such a read gives nothing. Past
    ``deadline`` (``time.monotonic``) no further read starts: the facts read so far are
    returned with ``PARTIAL`` as the first note."""
    facts: list[df.Fact] = []
    notes: list[str] = []
    words = " ".join(str(words or "").split())[:200]
    if not words:
        return facts, ["nothing named"]

    def late() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise _Late()

    def timed(name: str, args: dict[str, Any]) -> Any:
        late()
        return call(name, args)
    try:
        _gather(timed, late, words, terms, repo, before, facts, notes)
    except _Late:
        notes.insert(0, PARTIAL)
    return facts, notes


def _gather(call: Callable[[str, dict[str, Any]], Any], late: Callable[[], None],
            words: str, terms: df.Terms,
            repo: Any, before: Optional[str], facts: list[df.Fact], notes: list[str]) -> None:
    try:
        found = call("plx.find", {"query": words, "limit": 5, "response_format": "concise"})
    except _Late:
        raise
    except Exception:  # noqa: BLE001 - a refused or failed read gives nothing
        found = {}
        notes.append("records not searched")
    listed = [c for c in (found or {}).get("candidates") or [] if isinstance(c, Mapping)
              and (c.get("object") or {}).get("object_type") not in DOCUMENT_TYPES]
    candidates = [c for c in listed if c.get("tier") in ("exact", "strong")]
    if not candidates:
        # only weaker matches: used when they are one record, never one picked of several
        leads = [c for c in listed if c.get("tier") == "lead"]
        candidates = leads if len(leads) == 1 else []
        if len(leads) > 1:
            notes.append(f"{len(leads)} records loosely match: none used")
    for c in candidates[:MAX_RECORDS]:
        oid = (c.get("object") or {}).get("object_id")
        if not oid or repo is None:
            continue
        late()
        try:
            got, more, _root = df.facts_from(repo, oid, terms)
        except _Late:
            raise
        except Exception:  # noqa: BLE001 - a record not read gives nothing
            notes.append(f"{oid.split(':', 1)[0]} not read")
            continue
        facts += got
        notes += more
    if len(candidates) > MAX_RECORDS:
        notes.append(f"{len(candidates)} records match; the first {MAX_RECORDS} read")
    try:
        docs = call("plx.documents", {"query": words, "limit": MAX_DOCUMENTS * 2})
    except _Late:
        raise
    except Exception:  # noqa: BLE001
        docs = {}
        notes.append("documents not searched")
    listed = [d for d in (docs or {}).get("documents") or (docs or {}).get("results") or []
              if isinstance(d, Mapping) and d.get("citation")]
    if before:
        later = [d for d in listed if _day(d) and _day(d) >= before[:10]]
        listed = [d for d in listed if d not in later]
    for d in listed[:MAX_DOCUMENTS]:
        handle = str(d["citation"])
        try:
            read = call("plx.read", {"citation": handle})
        except _Late:
            raise
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(read, Mapping) or read.get("status") != "resolved":
            continue
        text = _text_of(read)
        facts += text_facts(text, handle) + quoted_prices(text, handle)


def text_answer(label: str, facts: Iterable[df.Fact]) -> tuple[str, Optional[df.Fact], str]:
    """A label answered from the documents' ``Label: value`` lines: ("filled", fact, ""),
    ("ambiguous", None, why) or ("no_fact", None, "")."""
    key = f"Text.{df.normalize(label)}"
    values: dict[str, list[df.Fact]] = {}
    for f in facts:
        if f.key == key and not f.ambiguous:
            values.setdefault(" ".join(f.value.lower().split()), []).append(f)
    if len(values) == 1:
        return "filled", next(iter(values.values()))[0], ""
    if len(values) > 1:
        return "ambiguous", None, f"the documents give {len(values)} different values"
    return "no_fact", None, ""


__all__ = ["MAX_DOCUMENTS", "MAX_RECORDS", "PARTIAL", "gather", "quoted_prices", "text_answer",
           "text_facts", "tier_key"]


# ---- a form's own answers: its thread, the filler's earlier copies, its record --------------
#
# A person filling a form usually has the answers next to it: the message that brought it
# and the rest of its thread (a customer's "PO 4471, lot B12" or an earlier copy in the same
# conversation), the copies of the same form they filled before (their own phone, account
# and rep lines never change), and the record the form's identifiers name. ``form_facts``
# reads those, nearest first, and ``plan_form`` fills a blank from the nearest source that
# answers it with exactly one value. A label is matched to what a source wrote by its
# meaning (``meaning``): the terms file's field for it, else its words with shortened
# words spelled out (``Terms.words``) and plurals dropped, so ``Lot #`` in a form reads a
# ``Lot Number:`` line in mail. Nothing is matched by a form's own list.
# Nearest of all: earlier versions of the same form in its own thread (the form sent back
# and forth, or the same form for another line of the job), read field by field like an
# earlier copy (its place and label), and used only where ``MIN_VERSIONS`` or more
# distinct versions write the same value and none writes another (measured on the forms'
# whole threads, fresh DEV split and the earlier 401: two or more versions agreeing, 153
# values, none wrong; a value only one version wrote, 50 wrong of 293).
# A value counts only when one of the versions writing it came from or went to someone
# outside the reader's own domain and its subdomains: versions passed only among colleagues
# are drafts, changed before the form goes out (offline over the DEV, TEST, slice 2 and 401
# threads, internal meaning the company's domains: a value one internal-only version
# wrote, 38 wrong of 168; one exchanged outside, 19 of 224; the one wrong fill of slice 2
# was three internal drafts agreeing; the rule costs 1 right fill there and 4 of 14 on
# the 401). The reader's domain unknown: no version counts.

TIERS = {0: "the record", 1: "the form's thread", 2: "your earlier copies of this form",
         3: "this form's earlier versions in its thread"}
THREAD, COPIES, VERSIONS = 1, 2, 3
MIN_VERSIONS = 2
SAME_FORM = 0.6             # shared labels over all labels: the same form
MAX_THREAD = 8              # thread messages read, newest first
MAX_THREAD_ATTACHMENTS = 10  # attachments read across the thread
# earlier copies: the newest MAX_COPIES the reader opens; a value is taken when MIN_COPIES or
# more write it and none writes another (held-out 401 forms, policy chosen on the sender's
# earlier copies only: 3 of the newest 5 agreeing, no dates, 646 of 648 right; 2 of 3:
# 695 of 726)
MAX_COPIES = 5
MIN_COPIES = 3
MIN_PARTIES = 2
MIN_SPAN = 30
# a copy's value of more than MAX_COPY_WORDS words is the filler's prose (a line about the
# business, a name with its qualifiers), which they reword form by form: never taken from
# copies (fresh TEST split: the one wrong copies fill was a long line the same in the 5
# newest copies to 3 parties, reworded on the form; over DEV, TEST and the 401: values of
# 9+ words 8 right, 1 wrong; of 8 or fewer, 39 right, none wrong)
MAX_COPY_WORDS = 8
MAX_IDENTIFIERS = 3
_CELL_ADDR = re.compile(r"^([A-Z]{1,3})(\d{1,7})=(.*)$", re.S)
_SHEET = re.compile(r"^\[Sheet: (.*)\]$")
_REVIEW_TAIL = re.compile(r"\s*\[Sheet [^\]]*; cells [^\]]*\]\s*$")
_FORMULA = re.compile(r"=FORMULA\(.*\); cached=")


def _canon_words(key: str, terms: df.Terms) -> str:
    out = []
    for w in key.split():
        w = terms.words.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


_FIELD_CANON: dict[int, dict[str, str]] = {}


def meaning(label: Any, terms: df.Terms) -> str:
    """What a label asks for: ``field:<name>`` when the terms file lists it (as written,
    or once its shortened words are spelled out), else its spelled-out words."""
    key = df.normalize(label)
    if not key:
        return ""
    if key in terms.fields:
        return "field:" + terms.fields[key][0]
    canon = _FIELD_CANON.get(id(terms))
    if canon is None:
        canon = {}
        for label_key, (name, _props) in terms.fields.items():
            canon.setdefault(_canon_words(label_key, terms), name)
        _FIELD_CANON[id(terms)] = canon
    words = _canon_words(key, terms)
    return "field:" + canon[words] if words in canon else words


def _blocks(text: str) -> list[dict[tuple[int, int], str]]:
    """A document's text as tables of cells: a workbook's rows by their cell addresses
    (``A1=x | B1=y``), a Word or Markdown table's rows (``| x | y |``: its first row is
    its header, kept as row 0), a line split at a tab or at three spaces or more; any
    other line is one cell."""
    out: list[dict[tuple[int, int], str]] = []
    sheet: Optional[dict[tuple[int, int], str]] = None
    table: Optional[dict[tuple[int, int], str]] = None
    for n, raw in enumerate(str(text or "").splitlines()):
        line = raw.strip()
        if _SHEET.match(line):
            sheet = {}
            out.append(sheet)
            table = None
            continue
        if not line:
            table = None
            continue
        parts = [p.strip() for p in line.strip("|").split(" | ")] if " | " in line or (
            line.startswith("|") and line.endswith("|")) else None
        if parts and all(_CELL_ADDR.match(p) for p in parts):
            if sheet is None:
                sheet = {}
                out.append(sheet)
            for p in parts:
                m = _CELL_ADDR.match(p)
                assert m is not None
                value = m.group(3)
                cached = _FORMULA.search(value)
                if cached:
                    value = value[cached.end():]
                    value = "" if value == "(missing)" else value
                sheet[(int(m.group(2)), df.column_number(m.group(1)))] = value
            continue
        if parts is not None and line.startswith("|"):
            if all(re.fullmatch(r":?-{3,}:?", p) for p in parts if p):
                continue
            if table is None:
                table = {}
                out.append(table)
            r = 1 + max((k[0] for k in table), default=-1)
            for c, p in enumerate(parts):
                table[(r, c + 1)] = p
            continue
        table = None
        cells = re.split(r"\t+| {3,}", line)
        out.append({(n + 1, c + 1): cell.strip() for c, cell in enumerate(cells) if cell.strip()})
    return out


def _clean_value(text: str) -> str:
    text = _REVIEW_TAIL.sub("", text.replace("\\_", "_").replace("\\*", "*"))
    text = " ".join(text.split()).strip(" :|")
    if not text or df._BLANKS.fullmatch(text) or not re.search(r"[A-Za-z0-9]", text):
        return ""
    return text[:df.MAX_VALUE]


def said(text: str, handle: str, wanted: Iterable[str], terms: df.Terms, tier: int
         ) -> list[df.Fact]:
    """Values a document's text writes for the meanings ``wanted``: ``Label: value`` in a
    line or cell, the cell right of a label, or the cell under a label in a row of labels.
    Each is a fact ``Said.<meaning>`` cited to the document, with its ``tier``."""
    want = set(wanted)
    out: list[df.Fact] = []

    def labelish(t: str) -> bool:
        m = meaning(t, terms)
        return bool(m) and (m in want or m.startswith("field:") or t.rstrip().endswith(":")
                            or df.normalize(t) in terms.person_only)

    def add(m: str, label: str, value: str) -> None:
        value = _clean_value(value)
        if value and len(out) < MAX_TEXT_FACTS:
            out.append(df.Fact(f"Said.{m}", value,
                               df.Citation(handle, " ".join(label.split())[:80], handle),
                               tier=tier))
    for block in _blocks(text):
        rows: dict[int, list[tuple[int, str]]] = {}
        for (r, c), cell in sorted(block.items()):
            rows.setdefault(r, []).append((c, cell))
        for r, cells in rows.items():
            # a table's header row (row 0) or a row of two labels or more: the value is
            # under the label
            header = r == 0 or sum(1 for _c, t in cells if t and labelish(t)) >= 2
            for i, (c, cell) in enumerate(cells):
                head, sep, rest = cell.partition(":")
                if sep and len(head) <= df.MAX_LABEL and rest.strip():
                    hm = meaning(head, terms)
                    if hm in want:
                        add(hm, head, rest)
                        continue
                if len(cell) > df.MAX_LABEL:
                    continue
                m = meaning(cell, terms)
                if m not in want:
                    continue
                right = next(((cc, t) for cc, t in cells[i + 1:] if t.strip()), None)
                if not header and right is not None and right[0] - c <= 4 \
                        and not labelish(right[1]):
                    add(m, cell, right[1])
                elif header:
                    below = block.get((r + 1, c), "")
                    if below and not labelish(below):
                        add(m, cell, below)
    return out


def _days(sent: str) -> Optional[int]:
    try:
        return datetime.date.fromisoformat(str(sent)[:10]).toordinal()
    except ValueError:
        return None


def _unanimous(per_copy: Sequence[Sequence[df.Fact]]
               ) -> dict[str, list[tuple[df.Fact, int]]]:
    """Per key: the facts (with their copy's index) when every copy writing it writes one
    and the same value and that value is no date (a copy's own day, however often it
    repeats); a key one copy writes twice differently gives nothing."""
    seen: dict[str, dict[str, list[tuple[df.Fact, int]]]] = {}
    for n, facts in enumerate(per_copy):
        mine: dict[str, set[str]] = {}
        for f in facts:
            mine.setdefault(f.key, set()).add(df._same(f.value))
        for key, values in mine.items():
            if len(values) != 1:
                seen.setdefault(key, {})["\0two"] = []      # two values in one copy
                continue
            first = next(f for f in facts if f.key == key)
            seen.setdefault(key, {}).setdefault(next(iter(values)), []).append((first, n))
    out = {}
    for key, values in seen.items():
        if len(values) == 1:
            got = next(iter(values.values()))
            if got and not any(v.type == "date" for v in values_in(got[0][0].value)):
                out[key] = got
    return out


def versions_agree(per_version: Sequence[Sequence[df.Fact]],
                   outside: Sequence[bool] = ()) -> list[df.Fact]:
    """From the earlier versions of a form in its thread (each version's facts, newest
    first): a field's value when ``MIN_VERSIONS`` or more write it, every version
    writing it writes the same (``_unanimous``) and at least one of them was exchanged
    with someone outside the reader's domain (``outside``, per version; missing is
    False), cited to the newest, tier ``VERSIONS``. A field the versions write
    differently is marked ambiguous: it changed within this job, so no other source
    answers it either."""
    agreed = _unanimous(per_version)
    out = [df.Fact(key, got[0][0].value, got[0][0].citation, tier=VERSIONS)
           for key, got in agreed.items() if len(got) >= MIN_VERSIONS
           and any(n < len(outside) and outside[n] for _f, n in got)]
    differ: dict[str, set[str]] = {}
    for facts in per_version:
        for f in facts:
            differ.setdefault(f.key, set()).add(df._same(f.value))
    out += [df.Fact(key, "", df.Citation("", key, ""), ambiguous=True, tier=VERSIONS)
            for key, values in differ.items() if len(values) > 1 and key not in agreed]
    return out


def copies_agree(per_copy: Sequence[Sequence[df.Fact]], parties: Sequence[str],
                 target: str = "", sent: Sequence[str] = ()) -> list[df.Fact]:
    """From the filler's earlier copies of a form (each copy's facts, newest first): a
    field's value when ``MIN_COPIES`` or more copies write it and every copy writing it
    writes the same, cited to the newest; a value that changes from copy to copy (a lot)
    or is a date is that copy's own and gives nothing. ``parties``: who each copy went to
    ("" unknown); the copies writing it must have gone to ``MIN_PARTIES`` or more (a value
    constant only across copies to one customer is that customer's), or all have gone to
    ``target``, the party this form is for, over a span of ``MIN_SPAN`` days or more
    (copies to one party within a few days are one job's, not a standing answer). A value
    of more than ``MAX_COPY_WORDS`` words gives nothing however often it repeats."""
    out = []
    for key, got in _unanimous(per_copy).items():
        if len(str(got[0][0].value).split()) > MAX_COPY_WORDS:
            continue
        who = {parties[n] for _f, n in got if n < len(parties) and parties[n]}
        when = [_days(sent[n]) if n < len(sent) else None for _f, n in got]
        standing = bool(target) and who == {target} and None not in when \
            and max(when) - min(when) >= MIN_SPAN    # type: ignore[type-var, operator]
        if len(got) >= MIN_COPIES and (len(who) >= MIN_PARTIES or standing):
            out.append(df.Fact(key, got[0][0].value, got[0][0].citation, tier=COPIES))
    return out


def plan_form(fields: Sequence[df.FormField], facts: Sequence[df.Fact], terms: df.Terms
              ) -> list[df.Filled]:
    """Each field's outcome from every source (``form_facts``): the record's rule first
    (``doc_fill.plan``); a blank it leaves is answered by the nearest tier that writes its
    meaning (the form's earlier versions in its thread, by the same field; the thread, only
    for a label the terms file lists, where the label's field is defined and not one mail
    never fills; then the filler's earlier copies; any other meaning only from a version
    or a copy), filled only when that tier gives one value. A record value and a thread
    value that differ leave the field ambiguous. Every fill carries its citation."""
    records = [f for f in facts if f.tier == 0 and not f.key.startswith(
        ("Text.", "Quoted.", "Said.", "Copy."))]
    said_facts = [f for f in facts if f.key.startswith(("Said.", "Copy."))]
    out = []
    for f, p in zip(fields, df.plan(fields, records, terms)):
        if p.status in ("kept", "person_only") or f.kind != "text":
            out.append(p)
            continue
        m = meaning(f.label, terms)
        pick: Optional[tuple[int, dict[str, list[df.Fact]]]] = None
        for tier in (VERSIONS, THREAD, COPIES):
            if tier == THREAD and (not m.startswith("field:")
                                   or m[6:] in terms.not_from_mail):
                continue
            # the thread by what the label means; a version or copy of the same form by
            # the same field (its place and label)
            key = f"Said.{m}" if tier == THREAD else \
                f"Copy.{f.field_id}|{df.normalize(f.label)}"
            values: dict[str, list[df.Fact]] = {}
            for x in said_facts:
                if x.tier == tier and x.key == key:
                    values.setdefault("\0ambiguous" if x.ambiguous else df._same(x.value),
                                      []).append(x)
            if values:
                pick = (tier, values)
                break
        if pick is None:
            out.append(p)
            continue
        tier, values = pick
        if "\0ambiguous" in values:
            out.append(df.Filled(f.field_id, f.label, "", "", "ambiguous", m, reason=(
                f"{TIERS[tier]} write it differently")))
            continue
        if len(values) > 1:
            out.append(df.Filled(f.field_id, f.label, "", "", "ambiguous", m, reason=(
                f"{TIERS[tier]} gives {len(values)} different values")))
            continue
        got = next(iter(values.values()))
        if tier == VERSIONS and m.startswith("field:") and m[6:] not in terms.not_from_mail \
                and any(x.tier == THREAD and x.key == f"Said.{m}"
                        and (x.ambiguous or df._same(x.value) != df._same(got[0].value))
                        for x in said_facts):
            # the thread says otherwise (a correction in mail after the versions)
            out.append(df.Filled(f.field_id, f.label, "", "", "ambiguous", m, reason=(
                f"{TIERS[VERSIONS]} and {TIERS[THREAD]} disagree")))
            continue
        if p.status == "filled":
            if df._same(p.after) != df._same(got[0].value):
                out.append(df.Filled(f.field_id, f.label, "", "", "ambiguous", p.field,
                                     reason=f"the record and {TIERS[tier]} disagree"))
            else:
                out.append(df.Filled(f.field_id, f.label, "", p.after, "filled", p.field,
                                     p.citation, (*p.also, got[0].citation)[:5]))
            continue
        if p.status == "ambiguous":
            out.append(p)
            continue
        # the versions tier is never certain (slice 2: three versions agreed on a line the
        # form then changed):
        # its value is suggested for the person to check and never written into the file
        out.append(df.Filled(f.field_id, f.label, "", got[0].value,
                             "suggested" if tier == VERSIONS else "filled", m,
                             got[0].citation, tuple(x.citation for x in got[1:5]),
                             reason=(f"suggested from {TIERS[tier]}; check it"
                                     if tier == VERSIONS else f"from {TIERS[tier]}")))
    return out


def _untrusted(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text", value.get("value"))
    return value if isinstance(value, str) else ""


def _base(handle: str) -> str:
    """A mail citation without its version (``@<change key>``)."""
    return str(handle).split("@", 1)[0]


def thread_of(call: Callable[[str, dict[str, Any]], Any], message: str
              ) -> tuple[list[str], str, Optional[str]]:
    """The citations of the messages in ``message``'s conversation the reader may read
    (``plx.read`` of the message for its conversation, ``plx.find`` for the mail holding
    exactly that conversation), its conversation id ("" when none is read), and when the
    message was sent (None when its read does not say)."""
    read = call("plx.read", {"citation": message, "max_chars": 200})
    if not isinstance(read, Mapping) or read.get("status") != "resolved":
        return [], "", None
    fields = read.get("fields") or {}
    conversation = _untrusted(fields.get("row.conversation_id"))
    sent = _untrusted(fields.get("row.sent_at")) or _untrusted(fields.get("row.received_at"))
    if not conversation or len(conversation) > 200:
        return [], conversation, sent or None
    found = call("plx.find", {"query": conversation, "limit": MAX_THREAD * 3,
                              "object_type": "Email"})
    return [c for c, _name in _exactly(found, conversation)], conversation, sent or None


def _exactly(found: Any, value: str) -> list[tuple[str, str]]:
    """(citation, display text) of the ``plx.find`` candidates whose matched value is
    exactly ``value`` (a conversation or message id), each citation once."""
    out: list[tuple[str, str]] = []
    for c in (found or {}).get("candidates") or []:
        if not isinstance(c, Mapping):
            continue
        if _untrusted((c.get("matched") or {}).get("raw")) != value:
            continue
        display = (c.get("object") or {}).get("display") or {}
        cited = (display.get("origin") or {}).get("citation") if isinstance(
            display, Mapping) else None
        if isinstance(cited, str) and cited.startswith("graph-mail://") \
                and _base(cited) not in {_base(h) for h, _n in out}:
            out.append((cited, _untrusted(display)))
    return out


_FORMLIKE = re.compile(r"\.(docx|xlsx|xlsm|pdf|csv|txt)$", re.I)


def attachments_of(read: Mapping[str, Any]) -> list[str]:
    """The citations of a message's Word, Excel, PDF and text attachments, as the reader's
    own ``plx.read`` of the message lists them (each read again by its citation, gated
    again)."""
    out = []
    for a in read.get("attachments") or ():
        if not isinstance(a, Mapping):
            continue
        cited, name = a.get("citation"), _untrusted(a.get("name"))
        if isinstance(cited, str) and cited.startswith("graph-mail://") \
                and _FORMLIKE.search(name.strip()) and cited not in out:
            out.append(cited)
    return out


def _version(read: Mapping[str, Any], cited: str, form: Sequence[df.FormField],
             terms: df.Terms, blob: Callable[[str], bytes], seen: set[str]
             ) -> Optional[list[df.Fact]]:
    """An attachment's filled fields (key ``Copy.<place>|<label>``, tier ``VERSIONS``) when
    it is a version of ``form`` (``SAME_FORM`` of their labels shared): its bytes by the
    digest the reader's own read of it gives, each digest once (the same file sent twice is
    one version); None when it is not one or cannot be opened."""
    digest = _untrusted((read.get("fields") or {}).get("row.content_sha256"))
    if not digest or digest in seen:
        return None
    seen.add(digest)
    try:
        theirs = df.open_document(blob(digest), terms).fields()
    except Exception:  # noqa: BLE001 - a file that cannot be opened is not a version
        return None
    mine = {df.normalize(f.label) for f in form} - {""}
    labels = {df.normalize(f.label) for f in theirs} - {""}
    if not mine or len(mine & labels) / len(mine | labels) < SAME_FORM:
        return None
    return [df.Fact(f"Copy.{f.field_id}|{df.normalize(f.label)}", f.current,
                    df.Citation(cited, f.label[:80], cited), tier=VERSIONS)
            for f in theirs if f.current.strip() and f.current != "="]


_ADDRESS = re.compile(r"[\w.+'-]+@([\w-]+(?:\.[\w-]+)+)")


def exchanged_outside(read: Mapping[str, Any], own: str) -> bool:
    """Whether a message, as the reader's ``plx.read`` gives it, came from or went to an
    address outside ``own``'s domain and its subdomains (``row.from``, ``row.to``,
    ``row.cc``). False when ``own`` is no address or the message names no address at all."""
    mine = own.rpartition("@")[2].strip().lower() if own.count("@") == 1 else ""
    if not mine:
        return False
    got = read.get("fields") if isinstance(read.get("fields"), Mapping) else {}
    domains = {d.lower() for path in ("row.from", "row.to", "row.cc")
               for d in _ADDRESS.findall(_untrusted(got.get(path)))}
    return any(d != mine and not d.endswith("." + mine) for d in domains)


def thread_facts(call: Callable[[str, dict[str, Any]], Any], message: str,
                 wanted: Iterable[str], terms: df.Terms, *, before: Optional[str] = None,
                 skip: Iterable[str] = (), form: Sequence[df.FormField] = (),
                 blob: Optional[Callable[[str], bytes]] = None, own: str = ""
                 ) -> tuple[list[df.Fact], list[str]]:
    """What the form's thread writes for the meanings ``wanted`` (``said``, tier
    ``THREAD``): its messages newest first, each as its text reads (``plx.read``); with
    ``form`` and ``blob`` (the capture store's bytes by digest), what the form's earlier
    versions among the attachments of messages sent no later than the form's own message
    agree on (``versions_agree``; ``own``: the reader's address, so a version exchanged only
    within their domain is told apart); no attachment's own text answers. ``before``: only
    messages sent earlier (a held-out measure); ``skip``: messages never read (the form's
    own, when it is the answer)."""
    want = set(wanted)
    notes: list[str] = []
    handles, conversation, sent_at = thread_of(call, message)
    at = before or sent_at     # the form's own time (a held-out measure's, else its mail's)
    if not conversation:
        return [], ["the form's message is not one you can read: its thread not searched"]
    skipped = {_base(h) for h in skip}
    reads = []
    for handle in handles:
        if _base(handle) in skipped:
            continue
        read = call("plx.read", {"citation": handle, "max_chars": 20000})
        if not isinstance(read, Mapping) or read.get("status") != "resolved":
            continue
        sent = _untrusted((read.get("fields") or {}).get("row.sent_at")) or \
            _untrusted((read.get("fields") or {}).get("row.received_at"))
        if before and (not sent or sent >= before):
            continue
        reads.append((sent, handle, read))
    reads.sort(key=lambda r: r[0], reverse=True)
    facts: list[df.Fact] = []
    versions: list[list[df.Fact]] = []
    outside: list[bool] = []
    digests: set[str] = set()
    room = MAX_THREAD_ATTACHMENTS
    for sent, handle, read in reads[:MAX_THREAD]:
        facts += said(_text_of(read), handle, want, terms, THREAD)
        if room <= 0 or not form or blob is None:
            continue
        if not at or not sent or sent > at:
            # sent after the form (or either time unknown): not an earlier version of it.
            # How long before does not decide: on live reads of the fresh DEV, TEST and
            # slice 2 forms, versions 31+ days older were 72 of 72 right
            continue
        out = exchanged_outside(read, own)
        for cited in attachments_of(read)[:room]:
            room -= 1
            # another document's text (a certificate, a packing list) states its own
            # lines, not this form's: an attachment answers only as a version of the
            # form (fresh DEV: 1 fill from attachment text, wrong)
            got = call("plx.read", {"citation": cited, "max_chars": 200})
            if isinstance(got, Mapping) and got.get("status") == "resolved":
                version = _version(got, cited, form, terms, blob, digests)
                if version is not None:
                    versions.append(version)
                    outside.append(out)
    facts += versions_agree(versions, outside)
    if len(reads) > MAX_THREAD:
        notes.append(f"the thread's {MAX_THREAD} newest messages read of {len(reads)}")
    return facts, notes


def sender_party(call: Callable[[str, dict[str, Any]], Any], message: Optional[str],
                 own: str) -> str:
    """The counterparty a form's email came from (``doc_style.party_of`` of its sender,
    read as the reader), the party a reply goes to; "" when the sender is the reader's own
    domain (``own``, an address), unreadable or not stated."""
    from operating import doc_style as ds
    if not message:
        return ""
    try:
        read = call("plx.read", {"citation": message, "max_chars": 200})
        got = _untrusted(((read or {}).get("fields") or {}).get("row.from"))
    except Exception:  # noqa: BLE001 - a sender not read names no party
        return ""
    found = re.search(r"[\w.+'-]+@[\w.-]+", got or "")
    return ds.party_of([found.group(0)] if found else [], [own.rpartition("@")[2]])


_IDENTIFIER = re.compile(r"(?i)\b([a-z][a-z .#/'-]{0,30}?)\s*[#:.]?\s*"
                         r"\b([A-Z]{0,4}[-#]?\d[A-Z0-9-]{1,30})\b")


def key_fields(terms: df.Terms) -> dict[str, tuple[str, ...]]:
    """The terms file's identifier fields (a name ending ``_number``), strongest first as
    listed, each with the record types it identifies."""
    out: dict[str, tuple[str, ...]] = {}
    for _label, (name, props) in terms.fields.items():
        if name.endswith("_number") and name not in out:
            out[name] = tuple(dict.fromkeys(p.split(".", 1)[0] for p in props))
    order = list(dict.fromkeys(n for n, _p in terms.fields.values()))
    return {n: out[n] for n in sorted(out, key=order.index)}


def identifiers(fields: Sequence[df.FormField], words: str, facts: Sequence[df.Fact],
                terms: df.Terms) -> list[tuple[str, str, Optional[str]]]:
    """(field name, identifier, the form field it was read from) the form names: its own
    filled identifier fields, then the request's words (``PO 4471``), then its thread's,
    strongest field first, each identifier once."""
    keys = key_fields(terms)
    found: list[tuple[str, str, Optional[str]]] = []
    for f in fields:
        m = meaning(f.label, terms)
        if m.startswith("field:") and m[6:] in keys and f.current.strip():
            found.append((m[6:], f.current.strip()[:60], f.field_id))
    for match in _IDENTIFIER.finditer(str(words or "")):
        head = match.group(1).split()
        for n in (3, 2, 1):
            m = meaning(" ".join(head[-n:]), terms) if len(head) >= n else ""
            if m.startswith("field:") and m[6:] in keys:
                found.append((m[6:], match.group(2), None))
                break
    for x in facts:
        if x.key.startswith("Said.field:") and x.key[11:] in keys and x.tier == THREAD:
            found.append((x.key[11:], x.value[:60], None))
    order = list(keys)
    seen: set[str] = set()
    out = []
    for name, ident, fid in sorted(found, key=lambda t: order.index(t[0])):
        norm = re.sub(r"[^A-Za-z0-9]", "", ident).lower()
        if norm and norm not in seen and re.search(r"\d", ident):
            seen.add(norm)
            out.append((name, ident, fid))
    return out


MAX_SAME_RECORDS = 3        # one business record in gold, FM and BC at most


def one_business_record(call: Callable[[str, dict[str, Any]], Any], ids: Sequence[str],
                        kind: str) -> bool:
    """Whether the records ``ids`` (of type ``kind``) are one business record: the
    reader's own ``plx.around`` over ``<kind>.same_record_as`` (either way) joins them all
    by verified edges. A lead edge, a link the type does not declare, a refused or failed
    read: not one."""
    if len(ids) < 2:
        return len(ids) == 1
    linked: dict[str, set[str]] = {i: set() for i in ids}
    for oid in ids:
        for direction in ("forward", "reverse"):
            try:
                got = call("plx.around", {"from": oid, "limit": 10, "path": [
                    {"link_type": f"{kind}.same_record_as", "direction": direction}]})
            except _Late:
                raise
            except Exception:  # noqa: BLE001 - a read that fails joins nothing
                return False
            if not isinstance(got, Mapping) or got.get("error"):
                return False
            for edge in got.get("edges") or ():
                if not isinstance(edge, Mapping) or edge.get("tier") != "verified":
                    continue
                a, b = str(edge.get("from")), str(edge.get("to"))
                if a in linked and b in linked and a != b:
                    linked[a].add(b)
                    linked[b].add(a)
    seen, todo = {ids[0]}, [ids[0]]
    while todo:
        for other in linked[todo.pop()] - seen:
            seen.add(other)
            todo.append(other)
    return seen == set(ids)


def subject_records(call: Callable[[str, dict[str, Any]], Any],
                    named: Sequence[tuple[str, str, Optional[str]]], terms: df.Terms
                    ) -> tuple[list[str], list[str], Optional[str]]:
    """The records the form's strongest identifiers name exactly (``plx.find`` read by the
    one identifier rule, ``fast_answer.by_identifier``): at most one of each record type,
    or the records of one business record its systems hold (``one_business_record``).
    An identifier naming two records otherwise, or two identifiers naming two different
    records of one type, is ambiguous: none of that type is used. Returns the record ids,
    notes, and the form field whose value named the first record (a key, never filled)."""
    from operating import fast_answer as fa
    keys = key_fields(terms)
    by_type: dict[str, set[str]] = {}
    groups: list[frozenset[str]] = []
    first_field: dict[str, Optional[str]] = {}
    notes: list[str] = []
    for name, ident, fid in named[:MAX_IDENTIFIERS]:
        types = keys[name]
        try:
            found = call("plx.find", {"query": ident[:200], "limit": 10})
            hit = fa.by_identifier(None, ident, types, found=found)  # type: ignore[arg-type]
        except fa.Unanswerable:
            continue
        except _Late:
            raise
        except Exception:  # noqa: BLE001 - a refused or failed lookup names nothing
            notes.append(f"{name.replace('_', ' ')} {ident} not looked up")
            continue
        if isinstance(hit, fa.Choices):
            ids = list(dict.fromkeys(str(c.get("object_id")) for c in hit.candidates))
            kinds = {str(c.get("object_type")) for c in hit.candidates}
            if len(kinds) == 1 and len(ids) <= MAX_SAME_RECORDS \
                    and one_business_record(call, ids, next(iter(kinds))):
                # the same business record held in several systems (their verified
                # same-record links join them all): read as one, a value they disagree
                # on is ambiguous (``doc_fill.plan``)
                kind = next(iter(kinds))
                by_type.setdefault(kind, set()).update(ids)
                groups.append(frozenset(ids))
                for oid in ids:
                    first_field.setdefault(oid, fid)
                continue
            notes.append(f"{name.replace('_', ' ')} {ident} names more than one record: "
                         "none used")
            for c in hit.candidates:
                by_type.setdefault(str(c.get("object_type")), set()).update(
                    {"\0many", str(c.get("object_id"))})
            continue
        obj = hit["object"]
        by_type.setdefault(obj["object_type"], set()).add(obj["object_id"])
        first_field.setdefault(obj["object_id"], fid)
    out = []
    for kind, ids in by_type.items():
        if len(ids) == 1:
            out.append(next(iter(ids)))
        elif frozenset(ids) in groups:
            out.extend(sorted(ids))
        else:
            notes.append(f"the form names {len(ids - {chr(0) + 'many'})} or more "
                         f"{kind} records: none used")
    key = first_field.get(out[0]) if out else None
    return out, notes, key


def form_facts(call: Callable[[str, dict[str, Any]], Any], fields: Sequence[df.FormField],
               terms: df.Terms, *, message: Optional[str] = None, words: str = "",
               repo: Any = None, before: Optional[str] = None, skip: Iterable[str] = (),
               copies: Sequence[df.Fact] = (), deadline: Optional[float] = None,
               find_records: bool = True, blob: Optional[Callable[[str], bytes]] = None,
               own: str = "") -> tuple[list[df.Fact], list[str], list[str]]:
    """Every source's facts for a form, nearest first (section doc): its thread (when the
    form came in mail: ``message``), the records its identifiers name (read through
    ``repo``, ``doc_fill.facts_from``), and ``copies`` (``doc_fill_copies.copy_facts``,
    read by the caller as the reader); ``find_records`` False when the reader named the
    record themselves; ``blob``: the capture store's bytes by digest, so the form's
    earlier versions in its thread are read field by field (``thread_facts``; ``own``: the
    reader's address, without which no version answers). Returns
    the facts, notes, and the records used.
    Past ``deadline`` no read starts and ``PARTIAL`` is the first note."""
    def late() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise _Late()

    def timed(name: str, args: dict[str, Any]) -> Any:
        late()
        return call(name, args)
    wanted = {meaning(f.label, terms) for f in fields} - {""}
    facts: list[df.Fact] = list(copies)
    notes: list[str] = []
    records: list[str] = []
    try:
        if message:
            try:
                got, more = thread_facts(timed, message, wanted, terms, before=before,
                                         skip=skip, form=fields, blob=blob, own=own)
            except _Late:
                raise
            except Exception:  # noqa: BLE001 - a thread not read gives nothing
                got, more = [], ["the form's thread not read"]
            facts += got
            notes += more
        if find_records:
            named = identifiers(fields, words, facts, terms)
            records, more, _key = subject_records(timed, named, terms)
            notes += more
        for oid in records:
            if repo is None:
                break
            late()
            try:
                got, more, _root = df.facts_from(repo, oid, terms)
            except _Late:
                raise
            except Exception:  # noqa: BLE001 - a record not read gives nothing
                notes.append(f"{oid.split(':', 1)[0]} not read")
                continue
            facts += got
            notes += more
    except _Late:
        notes.insert(0, PARTIAL)
    return facts, notes, records
