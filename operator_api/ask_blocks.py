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
