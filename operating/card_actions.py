"""Actions on a suggestion card: what a person may do about it, the exact message it would
send, and the confirmed, journalled doing of it.

A card (``operating.proactive_suggestions``, as the app states it) offers typed actions:

* outbound: ``send_email`` (the card's drafted message to the vendor, the customer or
  colleagues), ``reply_in_thread`` (the same words as a reply to the cited mail, so the
  thread is kept), ``forward`` (the cited mail to colleagues, with a note) and
  ``teams_message`` (a note to a configured Teams chat);
* local: ``create_follow_up`` (a reminder with a due date), ``mark_done``, ``snooze`` (until
  a date) and ``dismiss`` (with a reason).

``review`` turns a card, an action and the person's edits into the final payload (to, cc,
subject, body, the cited message it replies to) and states, before anything happens:
which cited facts the text still carries, every date or quantity in it that no citation or
drafted text backs (``uncited``: blocking, the person removes it), any recipient outside
the internal, vendor and customer domains known for the card (``unknown_domains``: one
more explicit confirm), and every refusal (``problems``). Hard refusals, fail closed:

* a sender or recipient who is an excluded person (``PLX_EXCLUDED_PEOPLE_OIDS``), and any
  internal address the exclusion guard cannot decide; an unavailable guard refuses every
  outbound action, for everyone;
* everyone an action reaches is checked as Graph addresses it: a reply's Reply-To (else
  the message's sender), read from the message itself (``Context.thread``; unreadable:
  refused, and the preview lists them), and every member of a Teams chat
  (``Context.chat_members``; unreadable: refused);
* an address on a tenant domain (the sender's, ``PLX_ACTIONS_INTERNAL_DOMAINS``, any domain
  the tenant directory holds an alias on, any ``*.onmicrosoft.com``) is decided by the
  guard, never assumed external;
* a reply or forward of a message that is not in the sender's own mailbox;
* a forward leaving the company; a Teams chat not configured for actions
  (``PLX_ACTIONS_TEAMS_CHATS``: the chats an admin cleared, name=id pairs);
* a figure, spelled-out number or relative day ("ten thousand", "tomorrow", "next week")
  that no citation or drafted text states, and a drafted figure moved to another label.

``ActionDesk.confirm`` does an action only on an explicit confirm by a named, signed-in
person, for the payload hash the preview showed them, once per idempotency key. Every
confirmed action is an append-only journal row (who, when, card, payload hash, recipients,
subject, citations, result), hash-chained, never holding the message body. An outbound
action is first journalled ``pending``; a second click, a second dialog or a retry after a
crash finds that row and never sends again; a send whose outcome is unknown (a 5xx, a cut
connection, a crash) blocks a resend until the person says they checked Sent Items. The send itself is
``integrations.graph_send.ActionSender``: a dry run (with what is missing) unless sending
is enabled and permitted.

Evidence label of tests: ``isolated-fixture`` / ``injected-transport``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA = "plx-card-actions/v1"
OUTBOUND = ("send_email", "reply_in_thread", "forward", "teams_message")
LOCAL = ("create_follow_up", "mark_done", "snooze", "dismiss")
KINDS = OUTBOUND + LOCAL
LABELS = {"send_email": "Send email", "reply_in_thread": "Reply in thread",
          "forward": "Forward", "teams_message": "Teams message",
          "create_follow_up": "Follow up", "mark_done": "Mark done", "snooze": "Snooze",
          "dismiss": "Dismiss"}
TEAMS_CHATS_ENV = "PLX_ACTIONS_TEAMS_CHATS"
INTERNAL_DOMAINS_ENV = "PLX_ACTIONS_INTERNAL_DOMAINS"
KNOWN_DOMAINS_ENV = "PLX_ACTIONS_KNOWN_DOMAINS"
MAX_RECIPIENTS = 10
MAX_SUBJECT = 255
MAX_BODY = 8000
MAX_NOTE = 500
FOLLOW_UP_DAYS, SNOOZE_DAYS = 3, 7
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_CHAT = re.compile(r"^19:[A-Za-z0-9_.-]{1,200}@(?:thread\.v2|unq\.gbl\.spaces)$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A date or a quantity in the text: a dated day first, then any number.
_SPELLED = (r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
            r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
            r"fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen|"
            r"half|couple")
_RELATIVE = (r"today|tonight|tomorrow|yesterday|overmorrow|"
             r"(?:next|this|last|coming|following)\s+(?:week|month|quarter|year|weekend|"
             r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
             r"end\s+of\s+(?:the\s+)?(?:day|week|month|quarter|year)|"
             r"monday|tuesday|wednesday|thursday|friday|saturday|sunday")
_TERM = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}|\d+(?:[.,]\d+)*|\b(?:{_RELATIVE})\b|"
                   rf"\b(?:{_SPELLED})\b", re.IGNORECASE)
# A figure and the word before it on the same line: "ordered 200", "by 2026-10-24".
_LABELLED = re.compile(r"([A-Za-z]+)[ \t:]{1,3}(\d{4}-\d{2}-\d{2}|\d+(?:[.,]\d+)*)")
_SENDER = re.compile(r"^[A-Za-z0-9._+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_=-]{1,400}$")


class ActionRefused(ValueError):
    """An action that cannot be confirmed; ``problems`` are plain sentences."""

    def __init__(self, problems: Sequence[str], status: int = 422) -> None:
        super().__init__("; ".join(problems))
        self.problems, self.status = list(problems), status


# --------------------------------------------------------------------------- the card

def _facts(card: Mapping[str, Any]) -> list[dict[str, str]]:
    out = []
    for fact in ((card.get("draft") or {}).get("facts") or ()):
        handle = (fact.get("source") or {}).get("handle") or fact.get("citation")
        if isinstance(handle, str) and fact.get("value") is not None:
            out.append({"fact": str(fact.get("fact")), "value": str(fact["value"]),
                        "handle": handle})
    return out


def mail_parts(handle: Any) -> tuple[str, str] | None:
    """(mailbox object id, Graph message id) of a ``graph-mail://`` message handle."""
    from integrations.live_source_guard import parse_handle
    try:
        (mailbox, message), _version, extra = parse_handle(handle, "graph-mail", 2)
    except (ValueError, TypeError):
        return None
    return None if extra else (mailbox.lower(), message)


def thread_handle(card: Mapping[str, Any]) -> str | None:
    """The cited mail a reply or forward answers: the vendor's latest cleared message."""
    for fact in _facts(card):
        if fact["fact"] == "last_vendor_message" and fact["handle"].startswith("graph-mail://"):
            return fact["handle"]
    return None


def offers(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The actions a card offers (kind, label, outbound), in the order the app shows them."""
    draft = card.get("draft") or {}
    kinds = []
    if draft.get("channel") in ("email", "internal_note"):
        kinds.append("send_email")
    if thread_handle(card):
        kinds += ["reply_in_thread", "forward"]
    kinds += ["teams_message", *LOCAL]
    return [{"kind": k, "label": LABELS[k], "outbound": k in OUTBOUND} for k in kinds]


def teams_chats(environ: Mapping[str, str]) -> list[dict[str, str]]:
    """The Teams chats configured for actions (``name=19:...@thread.v2`` pairs)."""
    chats = []
    for part in (environ.get(TEAMS_CHATS_ENV) or "").split(","):
        name, _, chat = part.strip().rpartition("=")
        if _CHAT.fullmatch(chat.strip()):
            chats.append({"name": name.strip() or "Teams chat", "chat_id": chat.strip()})
    return chats


# --------------------------------------------------------------------------- context

@dataclass
class Context:
    """Who acts and what decides recipients. ``exclusion``: the people-exclusion guard
    (``integrations.people_exclusion.PeopleExclusion``); ``today``: the local day."""
    actor_name: str
    sender: str | None                  # the signed-in person's own address
    sender_oid: str | None
    exclusion: Any
    today: date
    environ: Mapping[str, str] = field(default_factory=dict)
    # (mailbox oid, message id) -> {"from": address, "reply_to": [addresses] ([] none,
    # None unknown)} read from the message itself; None: unreadable (a reply is refused)
    thread: Callable[[str, str], Mapping[str, Any] | None] | None = None
    # chat id -> its members [{"id": oid, "email": address}]; None: unreadable (refused)
    chat_members: Callable[[str], Sequence[Mapping[str, Any]] | None] | None = None

    @property
    def internal_domains(self) -> frozenset[str]:
        """The sender's domain, the configured ones and every domain the tenant directory
        holds an address in (an alias on any of them is a tenant person's)."""
        own = {self.sender.rsplit("@", 1)[1].lower()} if self.sender and "@" in self.sender \
            else set()
        directory = getattr(self.exclusion, "known_domains", None)
        held = set(directory()) if callable(directory) else set()
        return frozenset(own | held | _domains(self.environ.get(INTERNAL_DOMAINS_ENV)))

    def is_internal(self, domain: str) -> bool:
        """A tenant domain: decided by the exclusion guard, never assumed external. Every
        ``*.onmicrosoft.com`` counts (the tenant's own default domain among them)."""
        domain = domain.lower()
        return domain in self.internal_domains or domain == "onmicrosoft.com" \
            or domain.endswith(".onmicrosoft.com")


def _domains(raw: Any) -> set[str]:
    return {d.strip().lower().lstrip("@") for d in str(raw or "").split(",") if d.strip()}


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[1].lower()


def default_payload(kind: str, card: Mapping[str, Any], ctx: Context) -> dict[str, Any]:
    draft = card.get("draft") or {}
    to = [draft["to"]["address"]] if (draft.get("to") or {}).get("address") else []
    note = f"{card.get('headline', '')}. Next: {(card.get('action') or {}).get('text', '')}"
    if kind == "send_email":
        return {"to": to, "cc": [], "subject": draft.get("subject"), "body": draft.get("body")}
    if kind == "reply_in_thread":
        return {"to": to, "cc": [], "subject": None, "body": draft.get("body")}
    if kind == "forward":
        return {"to": [], "cc": [], "subject": None, "body": f"For your attention: {note}"}
    if kind == "teams_message":
        chats = teams_chats(ctx.environ)
        return {"chat_id": chats[0]["chat_id"] if chats else None, "body": note}
    if kind == "create_follow_up":
        return {"due": (ctx.today + timedelta(days=FOLLOW_UP_DAYS)).isoformat(),
                "note": f"Follow up: {card.get('headline', '')}"[:MAX_NOTE]}
    if kind == "snooze":
        return {"until": (ctx.today + timedelta(days=SNOOZE_DAYS)).isoformat()}
    if kind == "dismiss":
        return {"reason": ""}
    return {"note": ""}


# --------------------------------------------------------------------------- review

def _addresses(values: Any, label: str, problems: list[str]) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[,;\s]+", values) if v]
    if not isinstance(values, (list, tuple)):
        problems.append(f"{label}: not a list of addresses.")
        return []
    out = []
    for value in values:
        address = str(value).strip().lower()
        if not _ADDRESS.fullmatch(address):
            problems.append(f"{label}: '{str(value)[:80]}' is not an email address.")
        elif address not in out:
            out.append(address)
    if len(out) > MAX_RECIPIENTS:
        problems.append(f"{label}: at most {MAX_RECIPIENTS} addresses.")
    return out


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:limit]


def _terms(text: str) -> list[str]:
    """Every figure, spelled-out number and relative day in ``text`` (lower case)."""
    return [m.group(0).lower() for m in _TERM.finditer(text or "")]


def uncited_terms(text: str, drafted: str, cited: Iterable[str]) -> list[str]:
    """The figures, spelled-out numbers and relative days of ``text`` that neither a cited
    value nor the drafted text states."""
    backed = set(_terms(drafted))
    for value in cited:
        backed |= set(_terms(value))
    return list(dict.fromkeys(t for t in _terms(text) if t not in backed))


def _labelled(text: str) -> list[tuple[str, str]]:
    return [(m.group(2), m.group(1).lower()) for m in _LABELLED.finditer(text or "")]


def swapped_figures(text: str, drafted: str) -> list[str]:
    """Figures of the drafted text now beside a label the draft gave another figure
    ("ordered 0 kg, received 200 kg" from "ordered 200 kg, received 0 kg")."""
    by_label: dict[str, set[str]] = {}
    for figure, label in _labelled(drafted):
        by_label.setdefault(label, set()).add(figure)
    figures = {f for held in by_label.values() for f in held}
    return list(dict.fromkeys(f"{label} {figure}" for figure, label in _labelled(text)
                              if label in by_label and figure in figures
                              and figure not in by_label[label]))


def _day(value: Any, label: str, low: date, high: date, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        problems.append(f"{label}: a date (YYYY-MM-DD) is needed.")
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        problems.append(f"{label}: not a real date.")
        return None
    if not low <= day <= high:
        problems.append(f"{label}: between {low.isoformat()} and {high.isoformat()}.")
        return None
    return value


def _excluded(guard: Any, value: str | None, internal: bool) -> str | None:
    """None when ``value`` is cleared, else why not (fail closed: an internal address the
    guard cannot decide is refused; an external one it cannot decide is no tenant
    person's)."""
    from integrations.people_exclusion import ExclusionUnavailable
    if not value:
        return None
    try:
        if guard.decides(value):
            return "excluded" if guard.is_excluded(value) else None
        guard.require()
    except ExclusionUnavailable:
        return "undecided"
    return "undecided" if internal else None


def payload_hash(card_id: str, kind: str, sender: str | None,
                 payload: Mapping[str, Any]) -> str:
    canon = json.dumps({"card": card_id, "kind": kind, "sender": sender, "payload": payload},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def review(card: Mapping[str, Any], kind: str, edits: Mapping[str, Any] | None,
           ctx: Context) -> dict[str, Any]:
    """The final payload of ``kind`` on ``card`` with the person's ``edits`` and every
    check of it (module doc). Unknown kinds and unknown edit fields are refused."""
    if kind not in KINDS:
        raise ActionRefused(["Unknown action."])
    if kind not in {o["kind"] for o in offers(card)}:
        raise ActionRefused([f"{LABELS[kind]} is not offered on this card."])
    base = default_payload(kind, card, ctx)
    # the thread's own recipients are read from the message, never taken from the edits
    edits = {k: v for k, v in (edits or {}).items()
             if v is not None and k != "thread_recipients"}
    if kind in ("reply_in_thread", "forward") and edits.get("in_reply_to") == thread_handle(card):
        del edits["in_reply_to"]   # echoed back from the preview: the card decides it
    unknown = set(edits) - set(base)
    if unknown:
        raise ActionRefused([f"Field not editable here: {', '.join(sorted(unknown))}."])
    raw = {**base, **edits}
    problems: list[str] = []
    payload: dict[str, Any] = {}
    unknown_domains: list[str] = []
    citations: list[dict[str, Any]] = []
    uncited: list[str] = []
    message_id = None
    if kind in OUTBOUND:
        guard = ctx.exclusion
        if guard is None or getattr(guard, "unavailable", "absent"):
            problems.append("The people-exclusion list is not available, so nothing can be "
                            "sent (fail closed).")
            guard = None
        if not ctx.sender:
            problems.append("No signed-in mailbox: sign in as yourself to send.")
        elif not _SENDER.fullmatch(ctx.sender):
            problems.append("Your mailbox address cannot be used as a sender.")
        elif guard is not None:
            verdict = _excluded(guard, ctx.sender_oid, True) if ctx.sender_oid \
                else _excluded(guard, ctx.sender, True)
            if verdict:
                problems.append("You cannot send from this mailbox." if verdict == "excluded"
                                else "Your mailbox could not be checked against the "
                                     "exclusion list.")
        draft_to = ((card.get("draft") or {}).get("to") or {}).get("address")
        known = _domains(ctx.environ.get(KNOWN_DOMAINS_ENV)) | (
            {_domain(draft_to)} if draft_to and "@" in draft_to else set())

        def check(address: str, *, internal_only: bool = False, oid: str | None = None
                  ) -> None:
            """One person the action reaches: never excluded, decided when internal."""
            inside = ctx.is_internal(_domain(address))
            if guard is not None:
                verdict = _excluded(guard, oid, True) if oid else None
                verdict = verdict or _excluded(guard, address, inside)
                if verdict == "excluded":
                    problems.append(f"{address} cannot receive messages from the Workbench.")
                elif verdict:
                    problems.append(f"{address} could not be checked against the exclusion "
                                    "list.")
            if internal_only and not inside:
                problems.append(f"A forward goes to colleagues only: {address} is outside "
                                "the company.")
            domain = _domain(address)
            if not inside and domain not in known and domain not in unknown_domains:
                unknown_domains.append(domain)

        if kind in ("reply_in_thread", "forward"):
            parts = mail_parts(thread_handle(card))
            if parts is None or not _MESSAGE_ID.fullmatch(parts[1]):
                problems.append("The cited message cannot be addressed.")
            elif not ctx.sender_oid or parts[0] != ctx.sender_oid.lower():
                problems.append("The cited message is not in your mailbox: send a new "
                                "email instead.")
            else:
                message_id = parts[1]
            payload["in_reply_to"] = thread_handle(card)
        if kind == "reply_in_thread":
            # Graph addresses a reply to the message's Reply-To, else its sender: those
            # are read from the message itself and checked like any recipient
            thread = ctx.thread(parts[0], parts[1]) if ctx.thread and message_id else None
            payload["thread_recipients"] = []
            if message_id and thread is None:
                problems.append("Who the reply goes to (the message's sender and Reply-To) "
                                "could not be read, so it cannot be checked.")
            elif thread is not None:
                reply_to = thread.get("reply_to")
                found = list(reply_to) if reply_to else [thread.get("from")]
                if reply_to is None or not all(isinstance(a, str) and _ADDRESS.fullmatch(a)
                                               for a in found):
                    problems.append("The message's Reply-To or sender is unknown, so the "
                                    "reply cannot be checked.")
                else:
                    payload["thread_recipients"] = list(dict.fromkeys(a.lower() for a in found))
        if kind != "teams_message":
            payload["to"] = _addresses(raw.get("to"), "To", problems)
            payload["cc"] = _addresses(raw.get("cc"), "Cc", problems)
            if not payload["to"]:
                problems.append("Add at least one recipient.")
            for address in dict.fromkeys(payload["to"] + payload["cc"]
                                         + payload.get("thread_recipients", [])):
                check(address, internal_only=kind == "forward")
        if kind == "send_email":
            payload["subject"] = _text(raw.get("subject"), MAX_SUBJECT + 1)
            if "\n" in payload["subject"] or not payload["subject"]:
                problems.append("The subject is one line and not empty.")
            elif len(payload["subject"]) > MAX_SUBJECT:
                problems.append(f"The subject is at most {MAX_SUBJECT} characters.")
        else:
            payload["subject"] = None
        if kind == "teams_message":
            chat = raw.get("chat_id")
            payload["chat_id"] = chat if isinstance(chat, str) else None
            if chat not in {c["chat_id"] for c in teams_chats(ctx.environ)}:
                problems.append("Pick a Teams chat configured for actions.")
            else:
                members = ctx.chat_members(chat) if ctx.chat_members else None
                if not members:
                    problems.append("The chat's members could not be read, so they cannot "
                                    "be checked against the exclusion list.")
                for member in members or ():
                    address, oid = member.get("email"), member.get("id")
                    if isinstance(address, str) and _ADDRESS.fullmatch(address):
                        check(address.lower(), oid=oid if isinstance(oid, str) else None)
                    elif guard is not None and isinstance(oid, str):
                        verdict = _excluded(guard, oid, True)
                        if verdict:
                            problems.append("A member of this chat cannot receive messages "
                                            "from the Workbench." if verdict == "excluded"
                                            else "A member of this chat could not be checked "
                                                 "against the exclusion list.")
                    else:
                        problems.append("A member of this chat could not be checked against "
                                        "the exclusion list.")
        payload["body"] = _text(raw.get("body"), MAX_BODY + 1)
        if not payload["body"]:
            problems.append("The message is empty.")
        elif len(payload["body"]) > MAX_BODY:
            problems.append(f"The message is at most {MAX_BODY} characters.")
        # every date, quantity, spelled-out number or relative day in the text: cited, or
        # in the drafted text (itself worded from cited values), and each figure still
        # beside the label it was drafted with; anything else the person typed is refused
        facts = _facts(card)
        text = f"{payload.get('subject') or ''}\n{payload['body']}"
        drafted = f"{base.get('subject') or ''}\n{base.get('body') or ''}"
        uncited = uncited_terms(text, drafted, [fact["value"] for fact in facts])
        swapped = swapped_figures(text, drafted)
        if uncited:
            problems.append("Not backed by a citation: " + ", ".join(uncited[:8])
                            + ". Use the cited value or remove it.")
        if swapped:
            problems.append("A cited figure moved to another label: " + ", ".join(swapped[:4])
                            + ". Keep each figure with what it was cited for.")
        citations = [{**fact, "present": fact["value"] in text} for fact in facts]
    elif kind == "create_follow_up":
        payload["due"] = _day(raw.get("due"), "Due", ctx.today,
                              ctx.today + timedelta(days=365), problems)
        payload["note"] = _text(raw.get("note"), MAX_NOTE)
    elif kind == "snooze":
        payload["until"] = _day(raw.get("until"), "Snooze until",
                                ctx.today + timedelta(days=1),
                                ctx.today + timedelta(days=90), problems)
    elif kind == "dismiss":
        payload["reason"] = _text(raw.get("reason"), MAX_NOTE)
        if not payload["reason"]:
            problems.append("Say why you dismiss it.")
    else:
        payload["note"] = _text(raw.get("note"), MAX_NOTE)
    return {"card_id": str(card["id"]), "kind": kind, "label": LABELS[kind],
            "outbound": kind in OUTBOUND, "sender": ctx.sender if kind in OUTBOUND else None,
            "payload": payload, "message_id": message_id, "citations": citations,
            "uncited": uncited, "unknown_domains": unknown_domains, "problems": problems,
            "payload_hash": payload_hash(str(card["id"]), kind,
                                         ctx.sender if kind in OUTBOUND else None, payload)}


# --------------------------------------------------------------------------- journal

class JournalBroken(RuntimeError):
    """The journal's hash chain does not hold: nothing more is done until it is repaired."""


class Journal:
    """Append-only, hash-chained JSONL (``path``; None: in memory). Each row carries the
    previous row's hash; ``rows`` re-checks the whole chain."""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._memory: list[str] = []
        self._lock = threading.Lock()

    @classmethod
    def for_state_dir(cls, state_dir: Path | None) -> "Journal":
        return cls(Path(state_dir) / "actions" / "journal.jsonl" if state_dir else None)

    def _lines(self) -> list[str]:
        if self.path is None:
            return list(self._memory)
        if not self.path.exists():
            return []
        return [line for line in self.path.read_text("utf-8").splitlines() if line.strip()]

    @staticmethod
    def _hash(row: Mapping[str, Any]) -> str:
        body = {k: v for k, v in row.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()

    def rows(self) -> list[dict[str, Any]]:
        out, prev = [], None
        for n, line in enumerate(self._lines(), 1):
            try:
                row = json.loads(line)
            except ValueError:
                raise JournalBroken(f"row {n} unreadable") from None
            if (not isinstance(row, dict) or row.get("seq") != n or row.get("prev") != prev
                    or row.get("hash") != self._hash(row)):
                raise JournalBroken(f"row {n} breaks the chain")
            out.append(row)
            prev = row["hash"]
        return out

    def locked(self) -> "_Held":
        return _Held(self)

    def _append(self, rows: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
        row = {**row, "seq": len(rows) + 1, "prev": rows[-1]["hash"] if rows else None}
        row["hash"] = self._hash(row)
        line = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if self.path is None:
            self._memory.append(line)
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        rows.append(row)
        return row


class _Held:
    """The journal held exclusively (thread lock and, on disk, ``flock``): read, decide,
    append, with no other writer in between."""

    def __init__(self, journal: Journal) -> None:
        self.journal, self._fd = journal, None

    def __enter__(self) -> "_Held":
        self.journal._lock.acquire()
        try:
            if self.journal.path is not None:
                self.journal.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._fd = os.open(self.journal.path.with_suffix(".lock"),
                                   os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self.rows = self.journal.rows()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        return self.journal._append(self.rows, row)

    def __exit__(self, *_exc: Any) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self.journal._lock.release()


# --------------------------------------------------------------------------- the desk

# Outcomes that may have gone out: never resent until the person checked Sent Items.
UNSURE = frozenset({"pending", "unknown"})


def _outcomes(rows: Sequence[Mapping[str, Any]], card_id: str, digest: str) -> dict[str, str]:
    """Each earlier send of this exact message (by idempotency key): its latest status."""
    out: dict[str, str] = {}
    for row in rows:
        if row["card_id"] == card_id and row["payload_hash"] == digest \
                and row["kind"] in OUTBOUND:
            out[row["idempotency_key"]] = row["status"]
    return out


def _at(rows: Sequence[Mapping[str, Any]], outcomes: Mapping[str, str], status: str) -> str:
    keys = {k for k, v in outcomes.items() if v == status}
    return next(r["at"] for r in reversed(rows) if r["idempotency_key"] in keys)


_LOCAL_STATUS = {"create_follow_up": "follow_up", "mark_done": "done", "snooze": "snoozed",
                 "dismiss": "dismissed"}


def _state_text(row: Mapping[str, Any]) -> str:
    status = row["status"]
    if status == "sent":
        return f"Sent by {row['actor']['name']}"
    if status == "dry_run":
        return "Not sent (dry run): " + "; ".join(row.get("missing") or ["sending is off"])
    if status == "failed":
        return "Send failed" + (f" (HTTP {row['http_status']})" if row.get("http_status")
                                else "") + ": nothing is retried"
    if status in UNSURE:
        return "Outcome unknown: check Sent Items before sending it again"
    if status == "follow_up":
        return f"Follow up due {row.get('due')}"
    if status == "snoozed":
        return f"Snoozed until {row.get('until')}"
    if status == "dismissed":
        return f"Dismissed: {row.get('reason')}"
    return "Done"


class ActionDesk:
    """Confirms, does and journals card actions (module doc). ``sender``: an
    ``integrations.graph_send.ActionSender``."""

    def __init__(self, journal: Journal, sender: Any, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.journal, self.sender = journal, sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def preview(self, card: Mapping[str, Any], kind: str, edits: Mapping[str, Any] | None,
                ctx: Context) -> dict[str, Any]:
        """``review`` plus, for an outbound action, the exact Graph request and what a live
        send still lacks (asked of the sender's gate; nothing is sent)."""
        from integrations.graph_send import request_for
        found = review(card, kind, edits, ctx)
        found["teams_chats"] = teams_chats(ctx.environ) if kind == "teams_message" else []
        found["request"], found["missing"] = None, []
        if kind in OUTBOUND and not found["problems"]:
            found["request"] = request_for(kind, ctx.sender, {**found["payload"],
                                                              "message_id": found["message_id"]})
            found["missing"] = self.sender.gate(kind, ctx.sender)[0]
        return found

    def confirm(self, card: Mapping[str, Any], kind: str, edits: Mapping[str, Any] | None,
                ctx: Context, *, confirmed: bool, idempotency_key: str,
                shown_hash: str | None, confirm_unknown_domains: bool = False,
                checked_sent_items: bool = False) -> dict[str, Any]:
        """Do ``kind`` once (module doc). Raises ``ActionRefused`` before anything happens
        when the confirm, the person, the payload or a rule does not hold. A message whose
        earlier send has an unknown outcome (a 5xx, a cut connection, a crash) is sent
        again only with ``checked_sent_items``: the person looked and it is not there."""
        if confirmed is not True:
            raise ActionRefused(["Nothing is done without your confirm."])
        if not ctx.actor_name or not (ctx.sender or ctx.sender_oid):
            raise ActionRefused(["Only a signed-in, named person can confirm an action."], 403)
        if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise ActionRefused(["The confirm carries no valid idempotency key."])
        found = review(card, kind, edits, ctx)
        if found["problems"]:
            raise ActionRefused(found["problems"])
        outbound = kind in OUTBOUND
        if outbound and shown_hash != found["payload_hash"]:
            raise ActionRefused(["The message changed since you previewed it: review it "
                                 "again before you send."], 409)
        if outbound and found["unknown_domains"] and confirm_unknown_domains is not True:
            raise ActionRefused(["Confirm the recipients outside the known vendor, customer "
                                 "and company domains: "
                                 + ", ".join(found["unknown_domains"]) + "."], 409)
        payload = found["payload"]
        row = {"schema": SCHEMA, "card_id": found["card_id"],
               "card_label": str((card.get("subject") or {}).get("label") or "")[:200],
               "kind": kind, "idempotency_key": idempotency_key,
               "payload_hash": found["payload_hash"],
               "actor": {"name": ctx.actor_name[:120], "email": ctx.sender},
               "to": payload.get("to") or [], "cc": payload.get("cc") or [],
               "subject": payload.get("subject"), "chat_id": payload.get("chat_id"),
               "in_reply_to": payload.get("in_reply_to"), "due": payload.get("due"),
               "until": payload.get("until"), "reason": payload.get("reason"),
               "note": payload.get("note"),
               "citations": [{k: c[k] for k in ("fact", "value", "handle")}
                             for c in found["citations"] if c["present"]]}
        try:
            with self.journal.locked() as held:
                same = [r for r in held.rows if r["idempotency_key"] == idempotency_key]
                if same:
                    if same[-1]["payload_hash"] != found["payload_hash"]:
                        raise ActionRefused(["This confirm was already used for a different "
                                             "action."], 409)
                    return {**_result(same[-1]), "replayed": True}
                if outbound:
                    before = _outcomes(held.rows, found["card_id"], found["payload_hash"])
                    if "sent" in before.values():
                        raise ActionRefused(["This exact message was already sent at "
                                             f"{_at(held.rows, before, 'sent')}."], 409)
                    if set(before.values()) & UNSURE and checked_sent_items is not True:
                        raise ActionRefused(["An earlier send of this message has an unknown "
                                             "outcome: check your Sent Items, then confirm "
                                             "that it is not there to send it again."], 409)
                    row["checked_sent_items"] = bool(before) and checked_sent_items is True
                    held.append({**row, "at": self._now(), "status": "pending"})
                else:
                    return _result(held.append({**row, "at": self._now(),
                                                "status": _LOCAL_STATUS[kind]}))
            sent = self.sender.dispatch(kind, ctx.sender,
                                        {**payload, "message_id": found["message_id"]})
            with self.journal.locked() as held:
                final = held.append({**row, "at": self._now(), "status": sent["status"],
                                     "missing": list(sent.get("missing") or ()),
                                     "http_status": sent.get("http_status")})
            return {**_result(final), "request": sent.get("request")}
        except JournalBroken:
            raise ActionRefused(["The actions journal failed its integrity check: nothing "
                                 "is done until it is repaired."], 503) from None

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _own(self, email: str | None, name: str) -> list[dict[str, Any]]:
        try:
            rows = self.journal.rows()
        except JournalBroken:
            return []
        return [r for r in rows if (r["actor"].get("email") or None) == email
                and (email is not None or r["actor"]["name"] == name)]

    def states(self, email: str | None, name: str, today: date) -> dict[str, dict[str, Any]]:
        """Each card's latest action by this person, as a status chip (an ended snooze
        counts no more)."""
        out: dict[str, dict[str, Any]] = {}
        for row in self._own(email, name):
            if row["status"] == "snoozed" and str(row.get("until")) < today.isoformat():
                out.pop(row["card_id"], None)
                continue
            out[row["card_id"]] = _result(row)
        return out

    def log(self, email: str | None, name: str, limit: int = 200) -> list[dict[str, Any]]:
        """This person's journal rows, newest first (a pending row its result follows is
        folded into that result)."""
        rows = self._own(email, name)
        ended = {r["idempotency_key"] for r in rows if r["status"] != "pending"}
        rows = [r for r in rows if r["status"] != "pending" or r["idempotency_key"] not in ended]
        return [_log_row(r) for r in reversed(rows)][:limit]


def _result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": row["status"], "text": _state_text(row), "at": row["at"],
            "kind": row["kind"], "card_id": row["card_id"], "seq": row["seq"],
            "missing": list(row.get("missing") or ()), "replayed": False}


def _log_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**_result(row), "card_label": row.get("card_label") or "",
            "actor": row["actor"]["name"], "actor_email": row["actor"].get("email"),
            "to": list(row.get("to") or ()),
            "cc": list(row.get("cc") or ()), "subject": row.get("subject"),
            "payload_hash": row["payload_hash"],
            "citations": list(row.get("citations") or ()),
            "http_status": row.get("http_status")}


def exclusion_for(environ: Mapping[str, str], state_dir: Path | None) -> Any:
    """The people-exclusion guard an action is checked by: the environment's list with
    the mail capture's recent directory attached (unavailable without a list)."""
    from integrations import people_exclusion as pe
    return pe.with_directory(pe.PeopleExclusion.from_env(environ), state_dir)


def card_dict(items: Iterable[Any], card_id: str) -> dict[str, Any] | None:
    """The reader's own card ``card_id`` from their suggestions (API models or dicts)."""
    for item in items:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        if str(data.get("id")) == card_id:
            return data
    return None
