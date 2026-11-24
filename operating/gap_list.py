"""The gap list: what Ask and Genie asked for that no view or measure covers.

A read that names a view or a measure the catalogue does not hold is answered with a typed
error (the read is never refused for it: the model answers from what is covered). This list
keeps each such name with when it was asked, so the same hole is counted, not rediscovered, and
a definition can be made for the ones asked most (research item 9: "asked 12 times, no
definition"). One JSON line per ask appended to one file; the error the reader sees says the
gap is on the list and how often it has been asked. A list that cannot be written says so in
the same error (no silent failure).

    python3 -m operating.gap_list            # the gaps, most asked first
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)
ENV = "PLX_GAP_LIST"
SUGGESTED = "/data/plx-state/ask-gaps/gaps.jsonl"   # what the API's environment sets it to
KINDS = ("view", "measure")


def path() -> Optional[Path]:
    """The list's file: only where ``PLX_GAP_LIST`` names it (the API's environment), so a test
    or a script that meets an unknown view never writes the real list."""
    given = os.environ.get(ENV)
    return Path(given) if given else None


def record(kind: str, name: str, now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """Append one ask of ``name`` (a ``kind`` of view or measure nothing covers); returns
    ``{"asked": N}`` (this one included) or ``{"error": why}`` when the list cannot be
    written."""
    if kind not in KINDS:
        raise ValueError(f"gap kind {kind!r}")
    name = str(name)
    at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    line = json.dumps({"at": at, "kind": kind, "name": name}, separators=(",", ":")) + "\n"
    p = path()
    if p is None:
        return {"error": f"the gap list is not switched on on this server ({ENV} is not set)"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        return {"asked": sum(1 for g in rows() if (g["kind"], g["name"]) == (kind, name))}
    except OSError as exc:
        log.error("gap list not written (%s): %s %s", p, kind, name, exc_info=True)
        return {"error": f"the gap list could not be written ({exc.strerror or 'io'})"}


def rows() -> list[dict[str, Any]]:
    """Every ask on the list, oldest first (a damaged line is skipped and logged)."""
    p = path()
    try:
        text = p.read_text(encoding="utf-8") if p is not None else ""
    except FileNotFoundError:
        return []
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        try:
            row = json.loads(raw)
            if row["kind"] in KINDS and isinstance(row["name"], str) and row["at"]:
                out.append(row)
                continue
        except (ValueError, KeyError, TypeError):
            pass
        log.error("gap list line %d unreadable: %.80r", n, raw)
    return out


def counts() -> list[dict[str, Any]]:
    """Per (kind, name): how many times asked, first and last, most asked first."""
    got: dict[tuple, dict[str, Any]] = {}
    for r in rows():
        g = got.setdefault((r["kind"], r["name"]), {"kind": r["kind"], "name": r["name"],
                                                   "asked": 0, "first": r["at"], "last": r["at"]})
        g["asked"] += 1
        g["last"] = max(g["last"], r["at"])
        g["first"] = min(g["first"], r["at"])
    return sorted(got.values(), key=lambda g: (-g["asked"], g["kind"], g["name"]))


def said(kind: str, name: str, got: dict[str, Any]) -> str:
    """The sentence the reader's error carries."""
    what = f"No {kind} named {name!r} exists."
    if "error" in got:
        return f"{what} It could not be put on the gap list: {got['error']}."
    return (f"{what} It is on the gap list (asked {got['asked']} time"
            f"{'' if got['asked'] == 1 else 's'}); answer from what is covered and say what is not.")


def main(argv: Optional[list[str]] = None) -> int:
    for g in counts():
        print(f"{g['asked']:>5}  {g['kind']:<8} {g['name']}  (first {g['first']}, last {g['last']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
