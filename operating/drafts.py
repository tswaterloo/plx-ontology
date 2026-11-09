"""One composer for every outgoing mail draft, in the sender's measured voice.

Every drafted message (a supplier date ask, a customer status note, an approval nudge,
the document pack reply, the daily digest...) is built here: the caller states the
sentences and the cited facts; ``compose`` adds the greeting and sign-off the sender uses
(company profile from sent mail: "Hi <first name>," when a person is on record, else no
greeting; no sign-off phrase, the mail client adds the signature), and runs the slop gate
(``copy_gate``) on the subject and the body. An optional sentence that fails is dropped;
a required one that fails leaves no draft (``body`` None) and the ``copy_check`` findings,
so the alert still shows and the draft button says "No draft". Nothing here sends.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Sequence

from . import copy_gate as gate

PURPOSES = ("supplier_date_ask", "supplier_expedite", "customer_status",
            "customer_delay_notice", "approval_nudge", "quote_followup",
            "document_pack_reply", "holding_reply", "digest", "playbook_ask",
            "meeting_followup")

_PERSON = re.compile(r"^[A-Z][a-z'\-]{1,30}$")


def first_name(name: Any) -> str | None:
    """A person's first name from a display name ("Sam Lee" -> "Sam", "Lee, Sam" ->
    "Sam"); None for a firm, an address or anything that is not plainly a person."""
    if not isinstance(name, str) or not name.strip() or "@" in name:
        return None
    text = name.strip()
    if gate._FIRM.search(text):
        return None
    if "," in text:
        text = text.split(",", 1)[1].strip()
    token = text.split()[0] if text.split() else ""
    return token if _PERSON.match(token) and len(text.split()) <= 3 else None


def greeting(first: str | None, voice: gate.VoiceProfile) -> str | None:
    if first and "hi" in voice.greetings:
        return f"Hi {first},"
    if "none" in voice.greetings:
        return None
    return "Hi,"


def signoff(voice: gate.VoiceProfile) -> str | None:
    mode = voice.signoffs[0] if voice.signoffs else "none"
    return {"none": None, "thanks": "Thanks", "thank_you": "Thank you",
            "regards": "Regards", "best": "Best"}.get(mode)


def compose(purpose: str, parts: Sequence[str], *, subject: str,
            facts: Sequence[gate.Fact] = (), person: str | None = None,
            voice: gate.VoiceProfile | None = None, required: int = 1,
            lines: Sequence[str] = (), today: date | None = None,
            specific: bool = True) -> dict[str, Any]:
    """``{"subject", "body", "copy_check"}``: ``parts`` (sentences, the first ``required``
    required) as one paragraph, then ``lines`` (record lines, one per line), framed in the
    sender's voice. ``specific`` False: the message deliberately states no figure and its
    subject names no record (a note to a customer about their account). ``person``: the recipient's display name when a person is on record
    (never a company: a firm name gives no greeting name). ``body`` is None when a required
    sentence or the subject fails the gate."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown draft purpose {purpose!r}")
    voice = voice or gate.VoiceProfile.company()
    subject_ok = gate.check(subject, surface="draft_subject", facts=facts, today=today,
                            specific=specific)
    head = greeting(first_name(person), voice)
    tail = signoff(voice)
    listed = "".join("\n- " + line for line in lines)

    def frame(prose: str) -> str:
        return "\n\n".join(x for x in (head, prose + listed, tail) if x)

    # the record lines are cited values the caller lists: facts the prose may repeat
    facts = list(facts) + [("line", line) for line in lines]
    kept = list(parts)
    result = gate.check(frame(" ".join(kept)), surface="draft_body", facts=facts,
                        voice=voice, today=today, specific=specific)
    while not result["ok"] and len(kept) > required:
        bad = _failing(kept, facts, voice, today)
        if bad is None:
            kept.pop()
        elif bad < required:
            break
        else:
            kept.pop(bad)
        result = gate.check(frame(" ".join(kept)), surface="draft_body", facts=facts,
                            voice=voice, today=today, specific=specific)
    ok = result["ok"] and subject_ok["ok"]
    check = {"ok": ok, "findings": [dict(f, part="subject") for f in subject_ok["findings"]]
             + [dict(f, part="body") for f in result["findings"]]}
    return {"purpose": purpose, "subject": subject if subject_ok["ok"] else None,
            "body": frame(" ".join(kept)) if ok else None, "copy_check": check}


def _failing(parts: list[str], facts: Sequence[gate.Fact], voice: gate.VoiceProfile,
             today: date | None) -> int | None:
    """The last part that fails on its own (None: only the whole fails, e.g. length)."""
    for n in range(len(parts) - 1, -1, -1):
        alone = gate.check(parts[n], surface="draft_body", facts=(), voice=voice, today=today)
        styled = [f for f in alone["findings"] if f["rule"] not in ("too_long",)]
        if styled:
            return n
        if facts:
            scoped = gate.check(parts[n], surface="draft_body", facts=facts, voice=voice,
                                today=today)
            if any(f["rule"] in ("unsupported_number", "unsupported_date")
                   for f in scoped["findings"]):
                return n
    return None


def as_text(draft: Mapping[str, Any]) -> str | None:
    return draft.get("body")
