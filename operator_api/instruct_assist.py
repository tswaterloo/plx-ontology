"""The Ask box's drafting (``instruct_api``) given what ``operating.assist`` reads in the
reader's own mail: who a new message goes to, and the record a short number the line
writes stands for. The same resolver the assist eval measures, never a copy.

* **The reader's own mail** (``assist.capture_history``: their own mailbox in the mail
  capture, sent and received; the people directory and internal domains from the
  captures, ``chunking.permissions.load_exclusion``) is read once per reader in the
  background and kept in memory for ``HISTORY_TTL`` seconds, or until the people
  exclusion changes (``pg.permissions.exclusion_stamp``). A large mailbox takes 30-50 s to
  read (measured 2026-11-13, 190-265k messages), so a draft never waits for a first read
  longer than ``RECIPIENT_WAIT`` past the model's reply: it is drafted without a proposal
  (as before), and the reader's next draft has it.
* **Recipients** (``assist.resolve``, run beside the model call): a new message's To is
  proposed only when the resolver settles it; several people of a name are offered, never
  picked; a reply keeps its thread's recipient. Every address passed the people exclusion
  in the resolver; the send desk checks each again at preview and confirm, and a proposed
  address never counts as a known domain there (``card_actions``).
* **Short record numbers** (``assist.with_thread``): "OR 120001" is read as the full number
  the reader's own mail writes it in, looked up on an exact match as any record the line
  names, and said, only when every recipient of the new message is settled and inside
  the company. A reply (its recipients are read only at the desk) or a message that may go
  outside does not read it: a short form may be another customer's order, so its facts
  never reach the draft.

Nothing here sends or writes; it reads only the reader's own mailbox. Evidence:
``isolated-fixture`` (injected history)."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as Timeout
from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Optional, Sequence

from operating import assist

HISTORY_TTL = 1800.0      # seconds a reader's mail history is kept
RECIPIENT_WAIT = 2.0      # seconds a draft waits for its recipients after the model replied
HISTORY_WAIT = 60.0      # seconds a resolution beside the model call waits for a first read
PARTY_SECONDS = 4.0       # all supplier/customer record reads of one resolution together
# words that point at the page, never name a recipient ("reply to this", "tell them")
_DEICTIC = frozenset({"this", "that", "these", "those", "it", "him", "her", "them", "me",
                      "us", "here", "there", "back", "all"})
# the mail reads and the resolutions waiting on them run in separate pools, so waiting
# resolutions never hold the workers a read needs
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="instruct-assist-read")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="instruct-assist")


@dataclass
class Aim:
    """What the assist read of one instruction: ``recipients`` (None: not resolved),
    ``notes`` to show beside the draft, ``expanded`` the full record numbers to read,
    ``records`` the records named it could not state (``unread_questions``)."""
    recipients: Optional[assist.Recipients] = None
    notes: list[str] = field(default_factory=list)
    expanded: list[str] = field(default_factory=list)
    records: list[dict[str, str]] = field(default_factory=list)   # ``unread_questions``


class _History:
    """Each reader's (people, own mail) read once in the background (module doc)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kept: dict[tuple[str, str], tuple[str, float, Future]] = {}

    def get(self, state: Path, oid: str, wait: float) -> Optional[tuple[Any, list]]:
        from operating.pg.permissions import exclusion_stamp
        stamp = exclusion_stamp(os.environ)
        key = (str(state), oid)
        with self._lock:
            kept = self._kept.get(key)
            if kept is None or kept[0] != stamp or time.monotonic() - kept[1] > HISTORY_TTL \
                    or (kept[2].done() and kept[2].exception() is not None):
                kept = (stamp, time.monotonic(), _READS.submit(_read_mail, state, oid))
                self._kept[key] = kept
        try:
            return kept[2].result(timeout=max(wait, 0.0))
        except Timeout:
            return None
        except Exception:  # noqa: BLE001 - unreadable: no proposal, drafted as before
            return None


def _read_mail(state: Path, oid: str) -> Optional[tuple[Any, list]]:
    from operating.chunking.permissions import load_exclusion
    people = load_exclusion(state, os.environ)
    if people is None or people.unavailable or not people.directory:
        return None
    own = {a for a, o in people.directory.items() if o == oid}
    return people, assist.capture_history(state, oid, own) if own else []


HISTORY = _History()


class InstructAssistMixin:
    """For ``InstructMixin``: needs ``state_dir`` (the captures) and may use ``reads`` (the
    reader's record reads, for a supplier's or customer's stated addresses) and
    ``_apa_roles`` (the autopilot's roles, for "the team"). Tests set
    ``instruct_history``: ``reader -> (people, own mail rows)`` or None."""

    instruct_history: Any = None

    def _assist_mail(self, reader: Any, wait: float) -> Optional[tuple[Any, list]]:
        if self.instruct_history is not None:
            return self.instruct_history(reader)
        state = getattr(self, "state_dir", None)
        oid = str(getattr(reader.session, "subject_oid", "") or "").lower()
        if not state or not oid:
            return None
        return HISTORY.get(Path(state), oid, wait)

    def _assist_intent(self, text: str, sent: list) -> Optional[assist.Intent]:
        try:
            intent = assist.parse(text)
        except assist.AssistRefused:
            return None
        intent.mentions = [w for w in intent.mentions if w.lower() not in _DEICTIC]
        return assist.with_thread(intent, sent)

    def _assist_resolve(self, reader: Any, intent: assist.Intent, people: Any, sent: list
                        ) -> Optional[assist.Recipients]:
        oid = str(getattr(reader.session, "subject_oid", "") or "").lower()
        if not (intent.mentions or intent.whole or intent.team):
            return None
        deadline = time.monotonic() + PARTY_SECONDS
        reads = getattr(self, "reads", None)

        def party(name: str, side: Optional[str]) -> set[str]:
            left = deadline - time.monotonic()
            if reads is None or not hasattr(reads, "run") or left <= 0:
                return set()
            from operating import fast_answer

            def job(view: Any) -> set[str]:
                access = view.access
                if not all(hasattr(access, n) for n in ("host", "runtime", "agent_server")):
                    return set()
                return assist.party_addresses(fast_answer.Reads(
                    access.host, access.runtime, access.actor, access.agent_server()),
                    name, side)
            try:
                return set(reads.run(reader, job, timeout=left) or ())
            except Exception:  # noqa: BLE001 - not readable now: no stated addresses
                return set()

        roles_of = getattr(self, "_apa_roles", None)
        try:
            roles = roles_of() if callable(roles_of) else {}
        except Exception:  # noqa: BLE001 - no roles file: no "team"
            roles = {}
        try:
            return assist.resolve(intent, me=oid, people=people, sent=sent, roles=roles,
                                  party=party)
        except assist.AssistRefused:
            return None

    def assist_before(self, reader: Any, text: str, replying: bool) -> Aim:
        """Before the materials are read: the short record numbers to read in full, when
        the reader's mail is already at hand (never waited for). ``replying``: the draft
        answers a message in its thread, whose recipients (its sender and Reply-To) are
        read only at the desk: it may go outside."""
        aim = Aim()
        mail = self._assist_mail(reader, 0.0)
        if mail is None:
            return aim
        people, sent = mail
        intent = self._assist_intent(text, sent)
        if intent is None or not intent.read_as:
            return aim
        internal = tuple(getattr(people, "internal", ()) or ())

        def inside(address: str) -> bool:
            domain = address.rsplit("@", 1)[-1].lower()
            return any(domain == d or domain.endswith("." + d) for d in internal)

        if replying:
            outside = True
        else:
            aim.recipients = self._assist_resolve(reader, intent, people, sent)
            # read in full only when every recipient is settled and inside the company;
            # unresolved, offered as choices or withheld (an excluded person, who may be
            # added by hand and whose address the reader does not see): not known
            outside = aim.recipients is None or aim.recipients.outside \
                or bool(aim.recipients.withheld) \
                or bool(aim.recipients.choices or aim.recipients.unresolved) \
                or not aim.recipients.to or not all(map(inside, aim.recipients.to)) \
                or not all(map(inside, aim.recipients.cc)) \
                or any(not inside(a) for q in aim.recipients.ask for a in q["candidates"])
        for short, full in intent.read_as.items():
            if outside:
                aim.notes.append(f"{short} may be {full} in your own mail; not read, as "
                                 "this message may go outside the company.")
            else:
                aim.notes.append(f"{short} was read as {full}, the form your own mail "
                                 "writes it in.")
                aim.expanded.append(full)
        return aim

    def assist_recipients(self, reader: Any, text: str) -> "Future[Optional[assist.Recipients]]":
        """A new message's recipients, resolved beside the model call (module doc)."""
        def work() -> Optional[assist.Recipients]:
            mail = self._assist_mail(reader, HISTORY_WAIT)
            if mail is None:
                return None
            people, sent = mail
            intent = self._assist_intent(text, sent)
            return None if intent is None else self._assist_resolve(reader, intent, people,
                                                                    sent)
        return _POOL.submit(work)


def settle(pending: "Optional[Future[Optional[assist.Recipients]]]", aim: Aim) -> Aim:
    """``aim`` with the recipients of ``pending`` when they came within ``RECIPIENT_WAIT``."""
    if aim.recipients is None and pending is not None:
        try:
            aim.recipients = pending.result(timeout=RECIPIENT_WAIT)
        except Exception:  # noqa: BLE001 - late or failed: drafted without a proposal
            aim.recipients = None
    return aim


def unread_questions(text: str, unread: Sequence[str], expanded: Sequence[str]
                     ) -> list[dict[str, str]]:
    """The records the line names (``assist.parse``: a year, a day range or a quantity is
    no record) that the draft could not state, each put to the reader (``RecordQuestion``):
    no record the reader may read holds it exactly (``not_found``), its read failed
    (``unreadable``), or it was named past the records read (``not_looked_up``); never left
    out in silence. A short number read in full is said by its full form only.
    ``unread``: a token as is, "?token" (read failed) or "+token" (past the reads)."""
    try:
        named = set(assist.parse(text, limit=1000).records) | set(expanded)
    except assist.AssistRefused:
        named = set(expanded)
    out = []
    for marked in dict.fromkeys(unread):
        how, token = (marked[0], marked[1:]) if marked[:1] in "?+" else ("", marked)
        if token not in named or any(e != token and e.endswith(token) for e in expanded):
            continue
        if how == "?":
            outcome, note = "unreadable", f"{token} could not be read just now: check it " \
                "before sending."
        elif how == "+":
            outcome, note = "not_looked_up", f"{token} was not looked up (only the first 3 " \
                "records named are read): check it before sending."
        else:
            outcome, note = "not_found", f"No record of {token} found in the records you " \
                "can read (Portal, Business Central, Sage, FileMaker): check the number " \
                "before sending."
        out.append({"record": token, "outcome": outcome, "note": note,
                    "ask_line": f"Could you confirm the details of {token}?"})
    return out


def unread_notes(text: str, unread: Sequence[str], expanded: Sequence[str]) -> list[str]:
    """``unread_questions``' notes, as words."""
    return [q["note"] for q in unread_questions(text, unread, expanded)]


def recipient_questions(recipients: Optional[assist.Recipients],
                        clean: Callable[[str], str]) -> list[dict[str, Any]]:
    """The recipient questions of a new message (``RecipientQuestion``), one tap each:
    the candidates ``assist.resolve`` offered, each through the guard's ``clean`` (one it
    blanks is never shown; a question left with none is dropped: ``address_notes`` says
    to check the recipients)."""
    out: list[dict[str, Any]] = []
    for q in (recipients.ask if recipients is not None else ()):
        shown = [a for a in q["candidates"] if clean(a) == a]   # the guard keeps it whole
        if not shown:
            continue
        if q.get("pick") == "one":
            label = f"Who is \"{clean(str(q.get('mention') or ''))}\"? Your mail does not " \
                "settle it"
        else:
            label = "Also copy? You often copy them on mail like this" \
                if q.get("field") == "cc" else \
                "Also send to? You often include them on mail like this"
        out.append({"field": "cc" if q.get("field") == "cc" else "to",
                    "pick": "one" if q.get("pick") == "one" else "any",
                    "label": label, "candidates": shown})
    return out


def address_notes(recipients: Optional[assist.Recipients],
                  clean: Callable[[str], str]) -> tuple[Optional[str], list[str]]:
    """(the proposed To, what to say about the recipients) for a new message. The To is
    proposed only when the reader's own mail settles it (``assist.resolve``); anything
    less is a question (``recipient_questions``: one tap each), never a guess."""
    if recipients is None:
        return None, ["Add the recipient's address before sending."]

    def listed(addresses: Iterable[str]) -> str:
        return ", ".join(x for x in (clean(a) for a in addresses) if x)
    notes: list[str] = []
    if any(not listed(q["candidates"]) for q in recipients.ask):
        notes.append("Check the recipients before sending.")
    notes += [f"\"{clean(who)}\" matches several people: pick one "
              f"({listed(options)})." for who, options in recipients.choices.items()
              if not any(q.get("mention") == who for q in recipients.ask)]
    notes += [f"No recipient found for \"{clean(who)}\"." for who in recipients.unresolved]
    if recipients.withheld:
        notes.append("Someone this message would go to can't be added here: check the "
                     "recipients.")
    if not recipients.to:
        return None, notes + ["Add the recipient's address before sending."]
    notes.insert(0, "The recipients are filled in from your own mail: check them before "
                    "sending.")
    return recipients.to[0], notes


__all__ = ["Aim", "HISTORY", "InstructAssistMixin", "address_notes", "recipient_questions",
           "settle", "unread_notes", "unread_questions"]
