"""Payment card numbers out of every text the Workbench shows, answers or sends a model.

Captured mail, mail images read by OCR and documents can carry a filled card authorisation
form. The capture keeps the source exactly as it is (capture is never modified); what the
Workbench serves passes ``mask_served`` / ``mask_served_in`` at its exits: every API
response and Ask stream (``operator_api.app``), every tool result an agent or model gets
(``operating.mcp_server``), every prompt handed to the ``claude`` CLI, Autopilot drafts,
exports and the alarm mail.

A card number is what ``operating.chunking.normalise`` already keeps out of the search
index (one rule, two renderings): 13-19 digits in a card layout (4-4-4-4, 4-4-4-4-3,
4-6-5 or 4-6-4 groups with one kind of separator, or 15-19 unbroken digits), inside a card
network's issued range and length, passing the Luhn check. An order, PO, tracking, phone or
barcode number is none: unbroken 13-14 digit runs are EAN/GTIN barcodes, a run continuing
a longer number is not a card, and a Luhn-valid number outside every network's range is
left as it is.

Shown as its last four digits (``•••• •••• •••• 1234``). Next to a masked card, on the
form that carries it, the security code (CVV, CVC, CID, CSC) and the expiry date are
masked too: a labelled value within ``NEAR`` characters, a bare ``MM/YY`` right after the
number, or the 3-4 digit group trailing a 4-4-4-4 card. The rule is idempotent and never
raises: masked text is left as it is, a non-string is returned unchanged.
"""

from __future__ import annotations

import bisect
import json
import re
from typing import Any

from operating.chunking.normalise import _CARD, _CARD_AFTER, _SEP, _issued, _luhn
from operating.secret_text import _LABEL, _TRIGGER, MASK, _credential, mask_secrets

DOT = "•"
NEAR = 160
# Cheap gate: no card fits in a text without 13 digits, nor without four digits in a row.
_FOUR = re.compile(r"\d{4}")
_MARK = re.compile(DOT * 4 + r" " + DOT * 4 + r" " + DOT * 4 + r" \d{4}")
# A label starts a word, or follows an escaped line break (``\n`` inside JSON text).
_START = r"(?:(?<=\\[nrt])|\b)"
_CODE = re.compile(r"(?P<label>" + _START + r"(?:CVV2?|CVC2?|CID|CSC|CVN|security\s+code|card\s+code|"
                   r"verification\s+(?:code|number|value)|3[- ]digit\s+code)\b"
                   r"[^\d\n]{0,24}(?:\n[^\d\n]{0,24})?)(?P<value>\d{3,4})(?!\d)", re.I)
_EXPIRY = re.compile(r"(?P<label>" + _START + r"(?:exp(?:iry|iration|ires)?\.?(?:\s+date)?|valid\s+"
                     r"(?:thru|through|until)|good\s+thru|expiration)\b"
                     r"[^\d\n]{0,24}(?:\n[^\d\n]{0,24})?)"
                     r"(?P<value>(?<!\d)(?:0?[1-9]|1[0-2])\s?[/.-]\s?(?:20)?\d{2}(?![\d/.-]\d))",
                     re.I)
_BARE_EXPIRY = re.compile(r"^(?P<label>[^\d\n]{0,20})(?P<value>(?:0[1-9]|1[0-2])\s?/\s?"
                          r"(?:20)?\d{2})(?![\d/])")
_TRAIL_CODE = re.compile(r"^(?P<label>[ \t]{1,3})(?P<value>\d{3,4})(?![\d/.,-])")


def _shown(digits: str) -> str:
    return f"{DOT * 4} {DOT * 4} {DOT * 4} {digits[-4:]}"


# A digit run glued to a letter is part of a code, a hash or a message id (``…a4111…b``),
# never a card, unless the letters before it are a card word an OCR reading ran into it.
_GLUED_WORD = re.compile(r"(?:card|visa|amex|mastercard|discover|cc|acct|account|number|"
                         r"num|no)$", re.I)
_LETTERS_BEFORE = re.compile(r"[A-Za-z]+$")


def _apart(text: str, start: int, end: int) -> bool:
    """Whether the run at ``start:end`` stands apart from the word around it."""
    after = text[end:end + 2]
    if after[:1].isalnum() or (after[:1] and after[:1] in "_@") \
            or (after[:1] in ".-_" and after[1:2].isalnum() and not after[1:2].isdigit()):
        return False
    before = text[start - 1:start] if start else ""
    if before and before in "_/=@":
        return False
    if before.isalpha():
        letters = _LETTERS_BEFORE.search(text, max(0, start - 20), start)
        return bool(letters and _GLUED_WORD.fullmatch(letters.group(0)))
    return True


def _card_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, digits)`` of every card number in ``text`` standing apart from the
    word around it, a trailing CVV group of a 4-4-4-4-3 run included in the span."""
    spans: list[tuple[int, int, str]] = []
    for found in _CARD.finditer(text):
        if not _apart(text, found.start(), found.end()):
            continue
        run = found.group(0)
        groups = _SEP.split(run)
        digits = "".join(groups)
        if _issued(digits) and _luhn(digits):
            spans.append((found.start(), found.end(), digits))
        elif len(groups) == 5 and len(digits) == 19 and _issued(digits[:16]) \
                and _luhn(digits[:16]):
            spans.append((found.start(), found.end(), digits[:16]))
    # ``_CARD`` matches never overlap, so their starts and ends both ascend: the one taken
    # span a later run can overlap is the last starting before it ends (bisect, linear
    # overall on a text dense with numbers)
    starts = [s for s, _, _ in spans]
    ends = [e for _, e, _ in spans]
    for found in _CARD_AFTER.finditer(text):
        before = bisect.bisect_left(starts, found.end())
        if (before and ends[before - 1] > found.start()) \
                or not _apart(text, found.start(), found.end()):
            continue
        digits = "".join(_SEP.split(found.group(0)))
        if _issued(digits) and _luhn(digits):
            spans.append((found.start(), found.end(), digits))
    return sorted(spans)


def _mask_value(match: re.Match[str]) -> str:
    value = match.group("value")
    return match.group("label") + re.sub(r"\d", DOT, value)


def mask_card_numbers(text: str) -> str:
    """``text`` with every payment card number shown as its last four digits and the
    security code and expiry next to it masked (module doc)."""
    if not text or not isinstance(text, str) or len(text) < 13 or not _FOUR.search(text):
        return text
    spans = _card_spans(text)
    if not spans:
        return text
    out: list[str] = []
    at = 0
    for start, end, digits in spans:
        if start < at:
            continue
        out.append(text[at:start])
        out.append(_shown(digits))
        at = end
        # right after the number: its trailing code group, or a bare MM/YY expiry
        rest = text[at:at + 40]
        trail = _TRAIL_CODE.match(rest) if len(digits) == 16 else None
        bare = _BARE_EXPIRY.match(rest)
        found = trail or bare
        if found is not None:
            out.append(_mask_value(found))
            at += found.end()
    out.append(text[at:])
    masked = "".join(out)
    # labelled security codes and expiry dates within NEAR of a masked card
    # (marks never overlap: starts and ends ascend, so the mark reaching furthest right
    # among those starting by ``start + NEAR`` is the last of them; bisect, not a scan)
    marks = [m.span() for m in _MARK.finditer(masked)]
    mark_starts = [s for s, _ in marks]

    def near(match: re.Match[str]) -> str:
        last = bisect.bisect_right(mark_starts, match.start() + NEAR)
        if last and marks[last - 1][1] + NEAR >= match.end():
            return _mask_value(match)
        return match.group(0)
    return _EXPIRY.sub(near, _CODE.sub(near, masked))


def mask_served(text: str) -> str:
    """``text`` as the Workbench may show it: card numbers (``mask_card_numbers``) and
    credential-shaped values (``operating.secret_text.mask_secrets``) masked.

    JSON text stays JSON (a tool result's text part, an Ask session's stream-json input):
    card masking only puts dots and blanks where digits were, and the credential masking
    of a JSON document is done on its parsed strings (``_secrets``), never across its
    quotes and escapes. (``secret_text`` itself is unchanged: the search index's chunk
    code reads it.)"""
    return _secrets(mask_card_numbers(text))


def _secrets(text: Any) -> Any:
    """``text`` through ``mask_secrets``; a JSON document string by string, re-serialised
    only when one changed (a string holding JSON again so), a string under a
    credential-named key (``"password": "..."``) masked as that label's value."""
    if not isinstance(text, str) or not _TRIGGER.search(text):
        return text
    if text.lstrip()[:1] in ("{", "["):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            value = _NOT_JSON
        if value is not _NOT_JSON:
            try:
                masked = _json_masked(value)
            except RecursionError:     # nested past what a walk may reach: as text
                return mask_secrets(text)
            if masked == value:
                return text
            return json.dumps(masked, ensure_ascii=False) + text[len(text.rstrip()):]
    return mask_secrets(text)


_NOT_JSON = object()
# a mapping key naming a credential (``password``, ``client secret``, ``MC_API_TOKEN``): its
# value is that label's value, as ``secret_text`` reads ``"key": "value"`` in text
_SECRET_KEY = re.compile(r"(?:.*[\s-])?" + _LABEL
                         + r"|(?-i:[A-Z][A-Z0-9_]*_(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD))",
                         re.IGNORECASE)


def _secret_field(key: str, value: str) -> bool:
    return bool(key) and len(value) >= 6 and bool(_SECRET_KEY.fullmatch(key.strip())) \
        and _credential(value)


def _json_masked(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        masked = _secrets(value)
        return MASK if masked == value and _secret_field(key, value) else masked
    if isinstance(value, dict):
        return {k: _json_masked(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_masked(v, key) for v in value]
    return value


def mask_served_in(value: Any) -> Any:
    """Every string inside a JSON-shaped value through ``mask_served`` (mapping keys
    kept; a tuple comes back a list, as JSON has it)."""
    if isinstance(value, str):
        return mask_served(value)
    if isinstance(value, dict):
        return {k: mask_served_in(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_served_in(v) for v in value]
    return value


def masked_spawn(spawn: Any) -> Any:
    """An agent session ``Spawn`` (``operating.agent_session``) whose task (in ``argv``)
    and fed input (the task with the host's first reads) go through ``mask_served``: no
    card number or credential reaches the model. Its ``stops``/``feeds`` abilities kept."""
    def masked(argv: list, cwd: str, env: dict, timeout: Any, **options: Any) -> Any:
        feed = options.get("feed")
        if feed is not None:
            options["feed"] = lambda: mask_served(feed())
        return spawn([mask_served(arg) for arg in argv], cwd, env, timeout, **options)
    for ability in ("stops", "feeds"):
        if getattr(spawn, ability, False):
            setattr(masked, ability, True)
    return masked


def masked_call(call: Any) -> Any:
    """A one-argument model call (a prompt in, a reply out) whose prompt goes through
    ``mask_served``; None stays None."""
    if call is None:
        return None
    return lambda prompt, *args, **kwargs: call(mask_served(prompt), *args, **kwargs)


def masked_mcp_line(line: bytes) -> bytes:
    """One MCP stdio message (a JSON line) on its way to a client with every string in a
    tool result masked; any other message, or a line that is not JSON, byte for byte."""
    if b'"result"' not in line:
        return line
    try:
        message = json.loads(line)
    except ValueError:
        return line
    result = message.get("result") if isinstance(message, dict) else None
    if not isinstance(result, dict) or "content" not in result:
        return line
    masked = mask_served_in(result)
    if masked == result:
        return line
    end = b"\n" if line.endswith(b"\n") else b""
    return json.dumps({**message, "result": masked}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + end


def has_card_number(text: Any) -> bool:
    """Whether ``text`` holds a payment card number (counts and audits; never prints it)."""
    return isinstance(text, str) and len(text) >= 13 and bool(_FOUR.search(text)) \
        and bool(_card_spans(text))


__all__ = ["DOT", "has_card_number", "mask_card_numbers", "mask_served", "mask_served_in",
           "masked_call", "masked_mcp_line", "masked_spawn"]
