"""Every Workbench model call, counted, and identical recent asks answered once.

``record(feature, ...)`` appends one line per model call to the ledger file
``PLX_MODEL_LEDGER`` names (mode 0600; unset: nothing is written, so a test's stand-in
model never counts as a call; the API unit sets it): when,
which feature, seconds, turns, tokens when the reply says, whether it ended well, whether
it was a reuse, and a short digest of the prompt (repeats are countable; the prompt itself,
the question and the answer are never written). The file is rotated past ``MAX_BYTES``
(one ``.1`` kept).

``reused(feature, prompt)`` / ``keep(feature, prompt, answer)``: a call whose prompt is
byte-identical to one answered within ``REUSE_SECONDS`` in this process gets that answer
again (recorded as a reuse). Only for a call with no tools whose prompt is its whole input
(query understanding, an alert rule's compile, a one-turn general answer): the same input,
so the same reading; never for an agent session (its tools read the store as it is now).
Failures are never kept.

``python3 -m operating.model_ledger [--hours N]``: calls, seconds, reuses and repeated
prompts per feature.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterable, Mapping

LEDGER_ENV = "PLX_MODEL_LEDGER"
MAX_BYTES = 20 * 1024 * 1024
REUSE_SECONDS = 300.0
MAX_REUSED = 256

_LOCK = threading.Lock()
_KEPT: "collections.OrderedDict[tuple[str, str], tuple[float, Any]]" = \
    collections.OrderedDict()


def path() -> Path | None:
    raw = os.environ.get(LEDGER_ENV, "").strip()
    return Path(raw) if raw else None


def digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def usage_of(reply: Any) -> tuple[int | None, int | None]:
    """(turns, tokens) a Claude Code JSON reply states, else None for either."""
    if not isinstance(reply, Mapping):
        return None, None
    turns = reply.get("num_turns") if isinstance(reply.get("num_turns"), int) else None
    usage = reply.get("usage") if isinstance(reply.get("usage"), Mapping) else {}
    tokens = sum(v for k, v in usage.items()
                 if k.endswith("_tokens") and isinstance(v, int)) or None
    return turns, tokens


def record(feature: str, *, seconds: float, ok: bool, prompt: str | None = None,
           turns: int | None = None, tokens: int | None = None, reused: bool = False,
           reason: str | None = None) -> None:
    """One ledger line; a ledger that cannot be written never fails the call."""
    line = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "feature": str(feature)[:60], "seconds": round(max(0.0, float(seconds)), 3),
            "ok": bool(ok), "reused": bool(reused)}
    if prompt is not None:
        line["prompt"] = digest(prompt)
    if turns is not None:
        line["turns"] = int(turns)
    if tokens is not None:
        line["tokens"] = int(tokens)
    if reason:
        line["reason"] = str(reason)[:80]
    target = path()
    if target is None:
        return
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                if target.stat().st_size > MAX_BYTES:
                    os.replace(target, target.with_name(target.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass


def reused(feature: str, prompt: str) -> tuple[bool, Any]:
    """(True, the answer) when this exact prompt of ``feature`` was answered within
    ``REUSE_SECONDS`` in this process (recorded as a reuse); else (False, None)."""
    key = (feature, digest(prompt) + ":" + str(len(prompt)))
    now = time.monotonic()
    with _LOCK:
        held = _KEPT.get(key)
        if held is None or held[0] <= now:
            if held is not None:
                del _KEPT[key]
            return False, None
        answer = held[1]
    record(feature, seconds=0.0, ok=True, prompt=prompt, reused=True)
    return True, answer


def keep(feature: str, prompt: str, answer: Any) -> None:
    """Keep a good answer for ``reused`` (None is never kept)."""
    if answer is None:
        return
    key = (feature, digest(prompt) + ":" + str(len(prompt)))
    with _LOCK:
        _KEPT[key] = (time.monotonic() + REUSE_SECONDS, answer)
        _KEPT.move_to_end(key)
        while len(_KEPT) > MAX_REUSED:
            _KEPT.popitem(last=False)


def summary(lines: Iterable[Mapping[str, Any]], since: dt.datetime | None = None
            ) -> dict[str, dict[str, Any]]:
    """Per feature: calls, reuses, failures, seconds, turns, tokens, and the calls whose
    prompt was asked before in the window (``repeats``: what reuse could still save)."""
    out: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for line in lines:
        try:
            at = dt.datetime.fromisoformat(str(line["at"]))
        except (KeyError, ValueError):
            continue
        if since is not None and at < since:
            continue
        feature = str(line.get("feature"))
        row = out.setdefault(feature, {"calls": 0, "reused": 0, "failed": 0, "seconds": 0.0,
                                       "turns": 0, "tokens": 0, "repeats": 0})
        row["calls"] += 1
        row["reused"] += bool(line.get("reused"))
        row["failed"] += not line.get("ok")
        row["seconds"] = round(row["seconds"] + float(line.get("seconds") or 0), 3)
        row["turns"] += int(line.get("turns") or 0)
        row["tokens"] += int(line.get("tokens") or 0)
        prompt = line.get("prompt")
        if prompt and not line.get("reused"):
            if prompt in seen[feature]:
                row["repeats"] += 1
            seen[feature].add(prompt)
    return out


def read_lines(target: Path | None = None) -> list[dict[str, Any]]:
    target = target or path()
    out: list[dict[str, Any]] = []
    if target is None:
        return out
    for file in (target.with_name(target.name + ".1"), target):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                out.append(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.model_ledger")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)
    json.dump(summary(read_lines(), since), sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
