"""An answer's question back and its declared gap (Genie plan v2 item 3): the Ask's model
asks the person to pick a reading when the question can be read two ways that give
different answers, and says plainly when nothing it can read holds what was asked. Both
are the model's own judgement over the dictionary and the values it read (no word lists,
no per-question rules); the app only reads the two fenced blocks it was told to write:

    ```clarify
    {"question": "Which Croda account?", "options": ["Croda Inc (US)", "Croda Canada"]}
    ```

    ```gap
    {"missing": "freight cost per shipment", "would_live": "carrier invoices"}
    ```

``take`` removes every such block from the answer's text and returns what they hold. A
block that is not one JSON object of that shape (unknown keys, wrong types, too long, fewer
than 2 or more than 4 options) is removed and nothing is taken from it (fail closed: no
question asked back, no gap claimed on words the app could not read). Only the first
clarify block is used (one question at a time); at most ``MAX_GAPS`` gaps.

A third block lists what the person has to act on (or any list of messages and records found),
grouped by what is asked of them, each item with where it comes from and a link:

    ```items
    {"groups": [{"title": "Reply", "items": [{"source": "Mail", "who": "Dana Reyes",
      "when": "24 Nov, 9:12 AM", "ref": "graph-mail://r.abcdefghij", "text": "Asks for the
      revised ship date on PO100412."}]}]}
    ```

``take_items`` removes each such block and returns the groups; ``link_items`` gives every
item's ``ref`` (a short ref or a handle a result of this conversation showed) its source
label and the link that opens it (``/source?handle=``); a ref no result showed is never
linked (the item stays, with no link). A block that is not that JSON shape stays in the
text unchanged, so nothing is lost; no count or length is limited.

A gap goes into the answer's ``not_found`` (its outcome ``not_found`` when nothing else was
answered); the answer, as every answer, is kept in the reader's work log, which is the gap
log (``work_log.Notes.ask``): who asked what that no record holds, readable per reader."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

MAX_QUESTION = 300
MAX_OPTION = 120
MAX_GAP = 300
MAX_GAPS = 3

_ITEMS = re.compile(r"```[ \t]*items[ \t]*\n(.*?)\n[ \t]*```[ \t]*\n?", re.S | re.I)
_BLOCK = re.compile(r"```[ \t]*(clarify|gap)[ \t]*\n(.*?)\n[ \t]*```[ \t]*\n?", re.S | re.I)

PROMPT_LINE = (
    "- When the question can be read two ways that give different answers (which of several "
    "matching records, parties, periods or measures) and neither the question, the "
    "conversation nor the records settle it, ask it back instead of answering: one short "
    "line on why, then a ```clarify block holding {\"question\": \"...\", \"options\": "
    "[\"...\", \"...\"]} (2 to 4 short readings the person can pick). When no view, measure "
    "or search source holds what is asked (the dictionary has no such data, or the values "
    "you read show it is not filled in for these rows), say so plainly, never answer "
    "another question in its place, and end with a ```gap block holding {\"missing\": "
    "\"what is not recorded\", \"would_live\": \"where it would be kept\"}.")


def _text(value: Any, most: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value if 0 < len(value) <= most else None


def _clarify(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) - {"question", "options"}:
        return None
    question = _text(raw.get("question"), MAX_QUESTION)
    options = raw.get("options")
    if question is None or not isinstance(options, list) or not 2 <= len(options) <= 4:
        return None
    labels = [_text(o, MAX_OPTION) for o in options]
    if any(lab is None for lab in labels) or len(set(labels)) != len(labels):
        return None
    return {"question": question, "options": labels}


def _gap(raw: Any) -> Optional[dict[str, str]]:
    if not isinstance(raw, dict) or set(raw) - {"missing", "would_live"}:
        return None
    missing = _text(raw.get("missing"), MAX_GAP)
    where = raw.get("would_live")
    where = _text(where, MAX_GAP) if where is not None else "a source the Workbench does not read"
    if missing is None or where is None:
        return None
    return {"what": missing, "where_it_would_live": where}


def take(text: str) -> tuple[str, Optional[dict[str, Any]], list[dict[str, str]]]:
    """(``text`` without its clarify and gap blocks, the question asked back or None, the
    gaps declared)."""
    asked: Optional[dict[str, Any]] = None
    gaps: list[dict[str, str]] = []
    for match in _BLOCK.finditer(text or ""):
        try:
            raw = json.loads(match.group(2))
        except ValueError:
            continue
        if match.group(1).lower() == "clarify":
            asked = asked or _clarify(raw)
        else:
            got = _gap(raw)
            if got and got not in gaps and len(gaps) < MAX_GAPS:
                gaps.append(got)
    return _BLOCK.sub("", text or "").strip(), asked, gaps


# ---------------------------------------------------------------- the items block

ITEMS_LINE = (
    "- Only for an open action or inbox list of things the person needs to act on now "
    "(what do I have to do, decide, reply to, or follow up on), put those action items in "
    "one ```items block: {\"groups\": [{\"title\": \"Decide\", \"items\": [{\"source\": "
    "\"Mail\", \"who\": \"name\", \"when\": \"24 Nov, 9:12 AM\", \"ref\": \"<the ref or "
    "handle a result showed for it>\", \"text\": \"one or two short lines: what is "
    "needed\"}]}]}. Groups by what the person must do (Decide, Reply, Follow up, Heads up), "
    "in that order; source: Mail, Teams, Planner, Mission Control, Business Central, "
    "Calendar...; quotes short. For factual or historical questions (such as what happened "
    "or what was promised), write the findings directly in prose or a markdown table in the "
    "answer text rather than hiding them in an items block.")

SOURCE_LABELS = {"graph-mail": "Mail", "teams-chat": "Teams", "teams-channel": "Teams",
                 "calendar-event": "Calendar", "meeting-transcript": "Meeting",
                 "sharepoint": "SharePoint", "sharepoint-list": "SharePoint list",
                 "original": "File"}


def _line(value: Any) -> Optional[str]:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def take_items(text: str) -> tuple[str, list[dict[str, Any]]]:
    """(``text`` without its readable items blocks, their groups in order). A block that is
    not JSON of the shape stays in the text."""
    groups: list[dict[str, Any]] = []

    def one(match: "re.Match[str]") -> str:
        try:
            raw = json.loads(match.group(1))
        except ValueError:
            return match.group(0)
        got = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(got, list) or not got:
            return match.group(0)
        out = []
        for g in got:
            items = g.get("items") if isinstance(g, dict) else None
            if not isinstance(items, list):
                return match.group(0)
            rows = []
            for i in items:
                what = _line(i.get("text")) if isinstance(i, dict) else None
                if what is None:
                    return match.group(0)
                rows.append({k: _line(i.get(k)) for k in ("source", "who", "when", "ref")}
                            | {"text": what})
            out.append({"title": _line(g.get("title")) or "", "items": rows})
        groups.extend(out)
        return ""

    return _ITEMS.sub(one, text or "").strip(), groups


def link_items(groups: list[dict[str, Any]], events: Any) -> list[dict[str, Any]]:
    """``groups`` with each item's ``open_url`` and source label, from the handles the
    conversation's own results showed (a short ref is the handle its result showed it
    for). Nothing is guessed: a ref no result showed gets no link, and the item keeps the
    source the answer named."""
    from urllib.parse import quote
    from operating import open_refs
    text = " ".join(json.dumps(e.get("result"), default=str) for e in events
                    if isinstance(e, dict) and e.get("type") == "tool_call"
                    and not e.get("is_error"))
    refs: dict[str, str] = {}
    open_refs.learn(text, refs)
    shown = set(open_refs._IN_TEXT.findall(text))
    out = []
    for g in groups:
        rows = []
        for i in g["items"]:
            ref = i.get("ref")
            handle = refs.get(ref) if ref in refs else ref if ref in shown else None
            row = {k: v for k, v in i.items() if k != "ref" and v}
            if handle:
                row["open_url"] = f"/source?handle={quote(handle, safe='')}"
                label = SOURCE_LABELS.get(handle.split("://", 1)[0])
                if label:
                    row["source"] = label
            rows.append(row)
        out.append({"title": g["title"], "items": rows})
    return out
