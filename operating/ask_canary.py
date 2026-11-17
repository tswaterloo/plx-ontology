"""The live Ask canary: one small fixed question with a known answer, asked through the
running Workbench API on this host's loopback (``POST /api/ask``, the route a
person's question takes; ``operating.canary_sender``, the one sender it may use) every 20
minutes (``plx-ask-canary.timer``), so a wrong, missing or slow Ask raises an alarm
(``ops_alarms`` check ``ask_canary``) instead of being found by a person (audit 11-15
row 9: an eval at 72% while nobody watched the live path).

    python3 -m operating.ask_canary run --state-dir STATE [--config FILE]

The question comes from the host's config (``/etc/plx/ask-canary.json``, never from code:
real cases are evaluation data). ``deploy/ops/install_keep_fresh.sh`` places the repo's
default (``deploy/ops/ask-canary.json``) there when the host has none:

    {"question": "...", "expect": ["...", ...], "max_seconds": 90}

The default asks where one long-closed Mission Control task is (a stable record: merged in
October, its stage never changed since; owned by the owner, no excluded person): a
retrieval, answered by the fast path (``operating.fast_answer`` ``task_status``: no model
step; 4.5 s median, 14.9 s p90 in the 11-15 router eval, against 110 s median for a live
agent Ask), so ``max_seconds`` 90 passes a healthy fast path and catches a fast path that
fell through to the agent. Cost per run: one fast answer (no model call while the fast path
answers), 72 a day.

The answer passes when every ``expect`` text appears (case and spacing ignored) in its
direct answer or its cited facts, the answer arrives within ``max_seconds`` and its
outcome is not a refusal. One question per run, nothing else asked: its cost is that one
answer, measured as its seconds and the steps the answer path took (a model step named),
kept in the status.

Sign-in: the canary's own service session, never the owner's. ``sudo -n plx-mint-link
--canary`` mints a console ticket marked ``ops_canary`` (``session_identity.SERVICE_CANARY``:
its signed ticket id says so; reads as the owner reader's claims, so the question's record
is visible) into the caller's ``~/.config/plx/ask-canary-access-link`` (``LINK``; the owner's
``workbench-access-link`` is never written or read here), then ``/api/auth/dev-login`` with
the ticket. The API serves that session ``GET /api/me`` and ``POST /api/ask`` only, writes
nothing of it to the owner's work log or memory and reads none of either for it. Each run
asks in a fresh conversation (no ``conversation_id``, no history), so no earlier canary
answer shapes the next. The session cookie is kept in ``STATE/ask-canary/session`` (0600)
and reused until the API refuses it (the ticket lives ``TTL_MINUTES``), so a link is minted
about three times a day. Nothing secret is printed.

Writes ``STATE/ask-canary/status.json`` (0600): the last runs, newest first, each with its
time, verdict, seconds, outcome and step names; never the answer's words (they hold
company values) nor the question. Read-only otherwise: the question asks, it never
confirms or sends anything. Exit 0 whatever the verdict (the alarm is the finding), 2 on
an unreadable state dir.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from .canary_sender import API_BASE, canary_request

SCHEMA = "plx-ask-canary/v1"
HOME, STATUS, SESSION = "ask-canary", "status.json", "session"
CONFIG_PATH = Path("/etc/plx/ask-canary.json")
MINT = "/usr/local/sbin/plx-mint-link"
# the canary's own link file (plx-mint-link --canary fixes this name); the owner's link
# file (OWNER_LINK) is never touched
LINK = Path.home() / ".config" / "plx" / "ask-canary-access-link"
OWNER_LINK = Path.home() / ".config" / "plx" / "workbench-access-link"
TTL_MINUTES = 480
KEEP = 12
DEFAULT_MAX_SECONDS = 90
REFUSED = ("refused", "not_found", "abstained")   # outcomes that are no answer


def mint_ticket() -> Optional[str]:
    """The canary service session's sign-in ticket from a freshly minted link; None when
    it cannot be minted. The link file is the canary's own (0600, ``LINK``); the ticket is
    never printed."""
    if LINK == OWNER_LINK:   # never the owner's link, whatever the paths become
        return None
    try:
        subprocess.run(["sudo", "-n", MINT, "--canary", str(TTL_MINUTES)], check=True,
                       timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        text = LINK.read_text().strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return text.split("#ticket=", 1)[1] if "#ticket=" in text else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def load_config(path: Path) -> Optional[dict[str, Any]]:
    """The canary's question; None when absent or malformed (fails closed: no question
    is never a pass)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("question"), str) \
            or not data["question"].strip():
        return None
    expect = data.get("expect")
    if not isinstance(expect, list) or not expect \
            or not all(isinstance(e, str) and e.strip() for e in expect):
        return None
    seconds = data.get("max_seconds", DEFAULT_MAX_SECONDS)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return None
    return {"question": data["question"].strip(), "expect": [e.strip() for e in expect],
            "max_seconds": float(seconds)}


def sse_events(lines: Iterable[bytes]) -> Iterable[tuple[str, Any]]:
    """(event, data) of a server-sent event stream."""
    event, data = "", []
    for raw in lines:
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            if event or data:
                try:
                    yield event, json.loads("\n".join(data)) if data else None
                except ValueError:
                    yield event, None
            event, data = "", []
        elif text.startswith("event:"):
            event = text[6:].strip()
        elif text.startswith("data:"):
            data.append(text[5:].lstrip())
    if event or data:
        try:
            yield event, json.loads("\n".join(data)) if data else None
        except ValueError:
            yield event, None


def answer_text(answer: Mapping[str, Any]) -> str:
    """The words a reader is shown as the answer: the direct answer and the cited facts'
    labels and values."""
    parts = [str(answer.get("direct_answer") or "")]
    for fact in answer.get("facts") or ():
        if isinstance(fact, Mapping):
            parts.append(str(fact.get("text") or ""))
    return " ".join(parts)


def judge(answer: Optional[Mapping[str, Any]], error: Optional[str], seconds: float,
          cfg: Mapping[str, Any]) -> tuple[bool, str]:
    """(passed, verdict): ``ok``, ``missing`` (no answer, an empty one or a refusal),
    ``wrong`` (an expected text absent), ``slow`` (over ``max_seconds``) or
    ``error:<code>``."""
    if error:
        return False, f"error:{error}"
    if not isinstance(answer, Mapping) or not str(answer.get("direct_answer") or "").strip() \
            or str(answer.get("outcome") or "") in REFUSED:
        return False, "missing"
    words = _norm(answer_text(answer))
    if not all(_norm(e) in words for e in cfg["expect"]):
        return False, "wrong"
    if seconds > cfg["max_seconds"]:
        return False, "slow"
    return True, "ok"


class Canary:
    def __init__(self, state: Path, *, transport: Callable[..., Any] = canary_request,
                 mint: Callable[[], Optional[str]] = mint_ticket,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.home = state / HOME
        self.transport, self.mint, self.clock = transport, mint, clock

    def _cookie(self) -> Optional[str]:
        try:
            value = (self.home / SESSION).read_text().strip()
        except OSError:
            return None
        return value or None

    def _keep(self, cookie: Optional[str]) -> None:
        path = self.home / SESSION
        if cookie is None:
            path.unlink(missing_ok=True)
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cookie)

    def _signed_in(self) -> Optional[str]:
        """A session cookie the API accepts: the kept one, else a new sign-in."""
        cookie = self._cookie()
        if cookie:
            status, _h, _b = self.transport("GET", API_BASE + "me", {"Cookie": cookie},
                                            None, 30)
            if status == 200:
                return cookie
        ticket = self.mint()
        if not ticket:
            return None
        status, headers, _b = self.transport(
            "POST", API_BASE + "auth/dev-login", {"Content-Type": "application/json"},
            json.dumps({"ticket": ticket}).encode(), 30)
        found = re.match(r"\s*([^=;\s]+=[^;]*)", str(headers.get("set-cookie") or ""))
        if status != 200 or not found:
            return None
        self._keep(found.group(1))
        return found.group(1)

    def ask(self, cfg: Mapping[str, Any]) -> dict[str, Any]:
        """One run: sign in, ask, judge. Never raises for an API problem: it is the
        finding."""
        started = self.clock()
        answer, error, steps = None, None, []
        try:
            cookie = self._signed_in()
            if cookie is None:
                error = "sign_in_failed"
            else:
                status, _h, body = self.transport(
                    "POST", API_BASE + "ask",
                    {"Content-Type": "application/json", "Cookie": cookie,
                     "Accept": "text/event-stream"},
                    # a fresh conversation each run: no history, no conversation id
                    json.dumps({"question": cfg["question"]}).encode(),
                    cfg["max_seconds"] * 2)
                if status == 401:
                    self._keep(None)   # next run signs in again
                if status != 200:
                    error = f"http_{status}"
                else:
                    for event, data in sse_events(body):
                        if event == "status" and isinstance(data, Mapping):
                            steps.append(str(data.get("step") or "")[:40])
                        elif event == "answer":
                            answer = data
                            break
                        elif event == "error":
                            error = str((data or {}).get("code") or "error")[:60]
                            break
                        if self.clock() - started > cfg["max_seconds"] * 2:
                            error = "timeout"
                            break
        except Exception as exc:  # noqa: BLE001 - an unreachable API is the finding
            error = type(exc).__name__
        seconds = round(self.clock() - started, 1)
        passed, verdict = judge(answer, error, seconds, cfg)
        return {"ok": passed, "verdict": verdict, "seconds": seconds,
                "outcome": (answer or {}).get("outcome") if isinstance(answer, Mapping)
                else None,
                "steps": steps[:20],
                # the answer path's model step (the fast path answers without one)
                "model": "answering" in steps,
                "answer_ms": (answer or {}).get("latency_ms") if isinstance(answer, Mapping)
                else None}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=1)
    os.replace(tmp, path)


def run(state: Path, config: Path, *, now: Optional[datetime] = None,
        canary: Optional[Canary] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    home = state / HOME
    home.mkdir(mode=0o700, exist_ok=True)
    try:
        held = json.loads((home / STATUS).read_text())
        runs = list(held.get("runs") or []) if isinstance(held, dict) else []
    except (OSError, ValueError):
        runs = []
    cfg = load_config(config)
    stamp = now.isoformat().replace("+00:00", "Z")
    if cfg is None:
        result: dict[str, Any] = {"at": stamp, "ok": False, "verdict": "not_configured"}
    else:
        result = dict((canary or Canary(state)).ask(cfg), at=stamp,
                      max_seconds=cfg["max_seconds"])
    status = {"schema": SCHEMA, "checked_at": stamp, "configured": cfg is not None,
              "runs": [result, *runs][:KEEP]}
    _write(home / STATUS, status)
    return status


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state-dir", required=True, type=Path)
    r.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    status = run(args.state_dir, args.config)
    last = status["runs"][0]
    print(json.dumps({k: last.get(k) for k in ("at", "verdict", "seconds", "outcome",
                                                "model")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
