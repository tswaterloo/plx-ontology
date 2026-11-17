"""Each person's own writing style, learned from the mail they sent, for the prose style hook
(``prose_style.set_person_style``): a draft written as a person and the humanize rewrite of
it (``operating.humanize``) follow how that person writes, not only the house style.

``StyleSource(state)`` is the provider: ``source(address)`` with the lower-case address a
draft is sent as returns ``{"rules": [...], "allow": [...]}`` or None. ``register(state)``
installs it (the API at start, ``operator_api.app.create_app``: the Ask's drafts and the
instruction drafts are the texts written as a person; the AI watcher's reports are written
as no one and take the house style; ``PLX_PERSON_STYLE=off`` leaves the house style alone).

What is read, per person, only when one of their drafts asks:

* **Whose.** The address must be a mailbox of the mail capture (its ``mailboxes`` table) and
  a person the people exclusion decides and does not keep out; the exclusion unavailable,
  undecided or excluding them: None (fail closed). Only that person's own mailbox is read.
* **Which mail.** Their most recent ``MAX_MESSAGES`` sent messages (the sender is the
  person; current versions; never a removed one; none naming an address the last purge
  could not decide; none to or copied to a person the exclusion keeps out or cannot
  decide), each reduced to the reply they wrote (quoted history cut,
  ``learning.reply_core``). The capture is opened read-only (``open_read_only``) and read
  through its sender index only: a store whose index is not filled yet gives None.
* **Learned style.** With at least ``MIN_MESSAGES``: the greeting and sign-off they use in
  ``SHARE`` of them or more, how long their replies run (median words), and whether they
  write em dashes (then the checker does not flag the em dashes in their drafts). The
  rules a person stated or the learner distilled (``learning.Lessons``, newest ledger)
  come first. Nothing for a message about pay or finance; no message words are kept, only
  the forms and the sign-off's own words.
* **Cached** per address for ``TTL`` seconds and per exclusion state, so a person's style
  costs one read per hour at most; a failed read is None and counts as no style.

Evidence: isolated-fixture (``operating/tests/test_person_style.py``); live-unverified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import threading
import time
from typing import Any, Mapping, Optional, Sequence

from . import learning as L

MAX_MESSAGES = 60
MIN_MESSAGES = 5
SHARE = 0.6
DASH_SHARE = 0.3
TTL = 3600.0
GUARD_TTL = 300.0
ROWS_READ = 4000          # sender-index rows read per person at most (newest kept)
OFF_ENV = "PLX_PERSON_STYLE"
_GREETING_TEXT = {"hi_name": "\"Hi <name>,\"", "hello_name": "\"Hello <name>,\"",
                  "hey_name": "\"Hey <name>,\"", "dear_name": "\"Dear <name>,\"",
                  "good_morning_name": "\"Good morning <name>,\"", "name_only": "the name alone",
                  "hi": "\"Hi,\"", "hello": "\"Hello,\"", "good_morning": "\"Good morning,\""}


def learned(replies: Sequence[str]) -> Optional[dict[str, Any]]:
    """The style rules of a person's own sent replies (module doc); None under
    ``MIN_MESSAGES`` usable replies."""
    usable = [r for r in (L.reply_core(x) for x in replies)
              if r and r.strip() and not L.finance_text(r)]
    if len(usable) < MIN_MESSAGES:
        return None
    n = len(usable)
    rules: list[str] = []
    greetings = [L.greeting_of(next((ln for ln in r.split("\n") if ln.strip()), ""))[0]
                 for r in usable]
    form, count = _top(greetings)
    if count / n >= SHARE:
        rules.append("Opens with no greeting, straight to the point." if form == "none" else
                     f"Opens with {_GREETING_TEXT.get(form, form)}.")
    signoffs = [L.signoff_of(r) for r in usable]
    form, count = _top([f for f, _ in signoffs])
    if count / n >= SHARE:
        if form == "none":
            rules.append("Ends without a sign-off line.")
        else:
            words = _top([w.rstrip(",.! ") for f, w in signoffs if f == form and w])[0]
            if words and len(words) <= 30:
                rules.append(f"Signs off with \"{words},\".")
    words = [len(" ".join(L.body_sentences(r)).split()) or len(r.split()) for r in usable]
    median = int(statistics.median(words))
    if median <= 40:
        rules.append(f"Keeps replies short: about {median} words, one or two sentences.")
    elif median >= 150:
        rules.append(f"Writes fuller replies, about {median} words.")
    else:
        rules.append(f"Replies run about {median} words.")
    allow = ["em dash"] if sum("—" in r for r in usable) / n >= DASH_SHARE else []
    return {"rules": rules, "allow": allow}


def _top(values: Sequence[Any]) -> tuple[Any, int]:
    counts: dict[Any, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None, 0
    best = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    return best


class StyleSource:
    """The ``prose_style.set_person_style`` provider over a state directory (module doc).
    ``guard``/``replies``: injected in tests (default: the people exclusion with the mail
    capture's directory; the capture's own sent mail)."""

    def __init__(self, state: Path, environ: Optional[Mapping[str, str]] = None, *,
                 guard: Any = None, replies: Any = None, lessons: Any = None,
                 clock: Any = time.monotonic) -> None:
        self.state = Path(state)
        self.environ = os.environ if environ is None else environ
        self._guard, self._replies, self._lessons = guard, replies, lessons
        self._clock = clock
        self._lock = threading.Lock()
        self._kept: dict[str, tuple[float, str, Optional[dict[str, Any]]]] = {}

    def __call__(self, address: str) -> Optional[dict[str, Any]]:
        address = str(address or "").strip().lower()
        if "@" not in address:
            return None
        guard = self._people()
        if guard is None:
            return None
        stamp = str(getattr(guard, "fingerprint", lambda: "")() or "")
        with self._lock:
            kept = self._kept.get(address)
        if kept and kept[1] == stamp and self._clock() - kept[0] < TTL:
            return kept[2]
        try:
            found = self._read(guard, address)
        except Exception:  # noqa: BLE001 - unreadable: house style only, retried next hour
            found = None
        with self._lock:
            self._kept[address] = (self._clock(), stamp, found)
        return found

    def _people(self) -> Any:
        """The exclusion, re-read every ``GUARD_TTL`` seconds (a draft never pays for it
        twice in a row); None when unavailable."""
        if self._guard is not None:
            return self._guard
        from .truth_check import people_guard
        held = getattr(self, "_held_guard", None)
        if held is not None and self._clock() - held[0] < GUARD_TTL:
            return held[1]
        try:
            guard, _why = people_guard(self.state, self.environ)
        except Exception:  # noqa: BLE001 - unreadable exclusion: nobody's style
            guard = None
        self._held_guard = (self._clock(), guard)
        return guard

    def _read(self, guard: Any, address: str) -> Optional[dict[str, Any]]:
        if not guard.decides(address) or guard.is_excluded(address):
            return None
        oid = self._mailbox(address)
        if oid is None or not guard.decides(oid) or guard.is_excluded(oid):
            return None
        stated = self._stated(oid)
        replies = (self._replies(oid, address) if self._replies is not None
                   else sent_replies(self.state, oid, address, guard))
        got = learned(replies or []) or {"rules": [], "allow": []}
        rules = list(dict.fromkeys([*stated, *got["rules"]]))
        if not rules and not got["allow"]:
            return None
        return {"rules": rules, "allow": got["allow"]}

    def _stated(self, oid: str) -> list[str]:
        from . import learning_fewshot as fs
        try:
            lessons = self._lessons if self._lessons is not None else L.Lessons.load(
                L.ledger_dir(self.state, self.environ), self.environ)
            return fs.rules(lessons.of(oid))
        except Exception:  # noqa: BLE001 - no ledger: the mail alone
            return []

    def _mailbox(self, address: str) -> Optional[str]:
        directory = getattr(self, "directory", None)
        if directory is None:
            directory = self.directory = mailbox_directory(self.state)
        return directory.get(address)


def mailbox_directory(state: Path) -> dict[str, str]:
    """Address -> mailbox id of the mail capture's mailboxes (read-only); {} unreadable."""
    import sqlite3
    path = Path(state) / "mail-capture" / "capture.db"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            for mailbox, upn, identities in db.execute(
                    "SELECT mailbox_id, upn, identities FROM mailboxes"):
                names = [upn]
                try:
                    names += [a for a in json.loads(identities or "[]") if isinstance(a, str)]
                except ValueError:
                    pass
                for a in names:
                    if isinstance(a, str) and "@" in a:
                        a = a.split(":", 1)[-1] if a.lower().startswith("smtp:") else a
                        out[a.lower()] = str(mailbox).lower()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return out


def sent_replies(state: Path, oid: str, address: str, guard: Any) -> list[str]:
    """The person's newest sent messages' words (module doc: which mail), read-only."""
    from integrations.capture_mail import MailCaptureStore, undecided_free_sql
    from ontology.people_directory import address_key
    store = MailCaptureStore.open_read_only(state)
    try:
        if not store._keyed():
            return []          # never the slow scan on a request path
        free = undecided_free_sql(store.db)
        rows = store.db.execute(
            "SELECT record, body_sha256 FROM messages INDEXED BY messages_by_sender "
            "WHERE mailbox_id = ? AND sender_key = ? AND superseded_at IS NULL AND "
            f"removed_reason IS NULL AND {free} ORDER BY version_id DESC LIMIT ?",
            (oid, address_key(address), ROWS_READ)).fetchall()
        picked: list[tuple[str, str]] = []
        for record, digest in rows:
            try:
                rec = json.loads(record)
            except ValueError:
                continue
            if not digest or str(rec.get("from") or "").strip().lower() != address:
                continue
            others = [str(a).lower() for a in [*(rec.get("to") or ()), *(rec.get("cc") or ()),
                                               *(rec.get("bcc") or ())]]
            if not others or any(not guard.decides(a) or guard.is_excluded(a)
                                 for a in others):
                continue        # to someone kept out, or not decidable: not read
            picked.append((str(rec.get("sent_at") or ""), digest))
        picked.sort(reverse=True)
        out = []
        for _at, digest in picked[:MAX_MESSAGES]:
            try:
                out.append(store.blob(digest).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 - a missing body: that message is left out
                continue
        return out
    finally:
        store.db.close()


def register(state: Optional[Path], environ: Optional[Mapping[str, str]] = None) -> bool:
    """Install the provider for ``state`` (True), or leave the house style alone (False:
    no state directory, or ``PLX_PERSON_STYLE=off``)."""
    from . import prose_style
    env = os.environ if environ is None else environ
    if state is None or str(env.get(OFF_ENV) or "").strip().lower() == "off":
        return False
    prose_style.set_person_style(StyleSource(Path(state), env))
    return True
