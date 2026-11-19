"""The header lines a model writes at the top of a drafted email ("To: ...", "Cc: ...",
"Subject: ..."), read as the draft's fields instead of left in its body. One reader for
both drafters (``operating.general_answer`` and ``operating.instruct``).

An address is kept only when the person's own words (``given``: the question or
instruction, and what the drafter was shown) write it: a model's guess at an address is
never proposed as a recipient. Pure text; nothing read or sent."""

from __future__ import annotations

import re
from typing import Iterable

_HEADER = re.compile(r"^\s*(to|cc|bcc|subject|re)\s*:\s*(.*?)\s*$", re.I)
_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
MAX_TO = 8


def addresses(text: str) -> list[str]:
    return list(dict.fromkeys(a.lower().rstrip(".") for a in _ADDRESS.findall(text or "")))


def split(inner: str, given: Iterable[str] = ()) -> tuple[str, list[str], list[str], str]:
    """(subject, to, cc, body) of a drafted email's text: the header lines at its top, in
    any order, until the first other line; blank lines between them and the body go. A
    ``Bcc`` line is dropped (the desk never sends one); an address no text of ``given``
    writes is dropped too."""
    stated = {a for g in given for a in addresses(str(g or ""))}
    subject, to, cc = "", [], []
    lines = (inner or "").strip("\n").split("\n")
    n = 0
    while n < len(lines):
        if not lines[n].strip():
            if n and any(_HEADER.match(x) for x in lines[:n]):
                n += 1
                continue
            break
        got = _HEADER.match(lines[n])
        if not got:
            break
        name, value = got.group(1).lower(), got.group(2)
        if name in ("subject", "re"):
            subject = subject or value
        elif name == "to":
            to += [a for a in addresses(value) if a in stated and a not in to]
        elif name == "cc":
            cc += [a for a in addresses(value) if a in stated and a not in cc]
        n += 1
    body = "\n".join(lines[n:]).strip()
    return " ".join(subject.split())[:200], to[:MAX_TO], [a for a in cc if a not in to][:MAX_TO], body


__all__ = ["addresses", "split"]
