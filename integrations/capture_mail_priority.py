"""What a mail run reads first: the scheduling bands of a run's units (:class:`MailPriority`) and
the automated-sender hint (split from ``integrations.capture_mail``, which re-exports every name)."""
from __future__ import annotations

from datetime import timedelta
import fcntl
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .capture_mail_scope import _stamp
from .capture_mail_standing import terminal_reason


# What changed in a held folder whose delta was last read
# over :data:`FRESH_SECONDS` ago; then the first-tier mailboxes' recent mail
# (``recent_days``), the scope's ``priority`` mailboxes first; their older history; every
# other mailbox, recent mail first; the deltas read within the hour; then the
# notification-heavy folders; then the folders with a terminal reason. Every band is still
# read in full. (A delta read again every turn would spend each budget on the same
# complete folders: test_capture_mail SchedulerTests.)
(FRESH_DUE, FIRST_RECENT, FIRST_HISTORY, OTHER_RECENT, OTHER_HISTORY, FRESH, BULK,
 LAST) = range(8)
BAND_NAMES = ("fresh_due", "first_recent", "first_history", "other_recent", "other_history",
              "fresh", "bulk", "terminal")
FRESH_SECONDS = 3600
# A folder is notification-heavy once this many of its current messages are held and one
# sender sends this share of them, or automated senders (``automated_sender``) this share.
BULK_SAMPLE = 200
BULK_TOP_SENDER = 0.8
BULK_AUTOMATED = 0.4
_AUTOMATED = re.compile(r"(?:no-?reply|do-?not-?reply|notification|notify|alert|"
                        r"mailer-daemon|postmaster|automated|bounce)", re.I)


def automated_sender(address: Any) -> bool:
    """Whether an address's local part names an automated sender (no-reply, alerts,
    notifications, bounces): a scheduling hint only, never a filter."""
    return isinstance(address, str) and "@" in address \
        and _AUTOMATED.search(address.rsplit("@", 1)[0]) is not None


class MailPriority:
    """The band of each unit of a run (see :data:`BAND_NAMES`), read from the store at
    the run's start: per folder its held senders and its oldest held message, per mailbox
    when it last received mail. A first-tier mailbox is one the scope's ``priority``
    names, else one whose account is enabled and that is active (mail received in the
    last ``active_days``, or none held yet: never read). ``rank`` orders the mailboxes of
    one band: the scope's ``priority`` mailboxes in its order, then all others together."""

    def __init__(self, store: Any, scope: Any, opened: Mapping[str, Sequence[str]],
                 *, disabled: Iterable[str] = ()) -> None:
        self.store = store
        now = store._clock()
        self.horizon = _stamp(now - timedelta(days=scope.recent_days))
        active_since = _stamp(now - timedelta(days=scope.active_days))
        self._update(store)
        newest: dict[str, str] = {}
        # Per folder the oldest received time held and the last version it covers
        # (``_recent`` then reads only the versions stored after it).
        self.oldest: dict[tuple[str, str], tuple[str | None, int]] = {}
        through = self._through(store)
        for row in store.db.execute("SELECT * FROM priority_folders"):
            key = (row["mailbox_id"], row["folder_id"])
            self.oldest[key] = (row["oldest"], through)
            if row["newest"] and row["newest"] > newest.get(row["mailbox_id"], ""):
                newest[row["mailbox_id"]] = row["newest"]
        senders: dict[tuple[str, str], list[tuple[int, bool]]] = {}
        for row in store.db.execute("SELECT mailbox_id, folder_id, automated, n "
                                    "FROM priority_senders"):
            senders.setdefault((row["mailbox_id"], row["folder_id"]), []).append(
                (row["n"], bool(row["automated"])))
        self.bulk = {key for key, held in senders.items() if self._bulk(held)}
        identities = {m: set() for m in opened}
        for row in store.db.execute("SELECT mailbox_id, upn, identities FROM mailboxes"):
            if row["mailbox_id"] in identities:
                identities[row["mailbox_id"]] |= {row["upn"].lower(), row["mailbox_id"],
                                                  *(str(i).lower() for i in
                                                    json.loads(row["identities"]))}
        named = {}
        for mailbox_id, known in identities.items():
            for position, address in enumerate(scope.priority_mailboxes):
                if address in known:
                    named[mailbox_id] = min(position, named.get(mailbox_id, position))
        off = {str(m).lower() for m in disabled}
        self.first = {m for m in opened if m in named or (
            m not in off and newest.get(m, active_since) >= active_since)}
        # The scope's priority mailboxes in its order; every other mailbox shares one rank,
        # so their folders take turns (every mailbox's newest pages before a second turn).
        self.rank = {m: named.get(m, len(scope.priority_mailboxes)) for m in opened}

    @staticmethod
    def _through(store: Any) -> int:
        row = store.db.execute("SELECT value FROM meta WHERE key='priority_through'"
                               ).fetchone()
        return int(row[0]) if row is not None else 0

    @classmethod
    def _update(cls, store: Any) -> None:
        """Add the versions stored since the last run to the aggregates (one pass over
        those only, by version id), so a run's start costs what was stored since.

        Disjoint mailbox captures may write the store beside each other. The aggregate
        itself is global, so its scan-and-watermark step is one critical section across
        every process of the store; the high watermark is read first, so versions written
        while the scan runs wait for the next pass rather than racing it."""
        lock_path = store.root / "priority-aggregate.lock"
        with open(lock_path, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            through = cls._through(store)
            high = store.db.execute("SELECT MAX(version_id) FROM messages").fetchone()[0] or 0
            folders: dict[tuple[str, str], list[str]] = {}
            senders: dict[tuple[str, str, str], list[int]] = {}
            for row in store.db.execute(
                    "SELECT version_id, mailbox_id, folder_id, json_extract(record, "
                    "'$.received_at') AS r, lower(json_extract(record, '$.from')) AS f "
                    "FROM messages WHERE version_id > ? AND version_id <= ? "
                    "ORDER BY version_id", (through, high)):
                key, received = (row["mailbox_id"], row["folder_id"]), row["r"] or ""
                if received:
                    span = folders.setdefault(key, [received, received])
                    span[0], span[1] = min(span[0], received), max(span[1], received)
                sender = row["f"] or ""
                held = senders.setdefault((*key, sha256(sender.encode("utf-8")).hexdigest()),
                                          [0, int(automated_sender(sender))])
                held[0] += 1
            if high == through:
                return
            with store.db:
                store.db.executemany(
                    "INSERT INTO priority_folders VALUES (?,?,?,?) ON CONFLICT (mailbox_id, "
                    "folder_id) DO UPDATE SET oldest=min(coalesce(oldest, excluded.oldest), "
                    "excluded.oldest), newest=max(coalesce(newest, excluded.newest), "
                    "excluded.newest)", [(*k, v[0], v[1]) for k, v in folders.items()])
                store.db.executemany(
                    "INSERT INTO priority_senders VALUES (?,?,?,?,?) ON CONFLICT (mailbox_id, "
                    "folder_id, sender) DO UPDATE SET n=n+excluded.n",
                    [(*k, v[1], v[0]) for k, v in senders.items()])
                store.db.execute("INSERT OR REPLACE INTO meta VALUES ('priority_through', ?)",
                                 (str(high),))

    @staticmethod
    def _bulk(held: Sequence[tuple[int, bool]]) -> bool:
        total = sum(n for n, _automated in held)
        if total < BULK_SAMPLE:
            return False
        automated = sum(n for n, is_automated in held if is_automated)
        return max(n for n, _automated in held) >= BULK_TOP_SENDER * total \
            or automated >= BULK_AUTOMATED * total

    def _recent(self, state: Mapping[str, Any] | None, folder_id: str | None) -> bool:
        """Whether the folder still has mail within ``recent_days`` to read: its windowed
        round is not done, or its history (read newest first) has not passed the horizon."""
        if state is None or not state["delta_url"] or state["next_url"]:
            return True
        if folder_id is None:
            return True
        key = (state["mailbox_id"], folder_id)
        oldest, through = self.oldest.get(key, (None, 0))
        # Only the versions stored since it last looked (the folder index, by version id).
        row = self.store.db.execute(
            "SELECT MIN(json_extract(record, '$.received_at')), MAX(version_id) FROM "
            "messages WHERE mailbox_id=? AND folder_id=? AND version_id > ?",
            (*key, through)).fetchone()
        if row[1] is not None:
            found = row[0]
            oldest = found if oldest is None or (found is not None and found < oldest) \
                else oldest
            self.oldest[key] = (oldest, row[1])
        return oldest is None or oldest > self.horizon

    def _delta_due(self, state: Mapping[str, Any] | None) -> bool:
        """Whether a held folder's delta is due: its round is current (a deltaLink, no
        frontier) and it was last read over :data:`FRESH_SECONDS` ago. Whatever its
        senders or status: a notification-heavy folder's new mail is read with every
        other folder's, and so is a folder still incomplete only for what lies behind its
        deltaLink (``history`` all backfilling its older mail, attachments deferred),
        else the history bands (millions of messages while ``history`` all backfills)
        starve its new mail for days (2026-11-02: 156 backfilling folders unread since
        30 Oct). A visit reads the delta before anything else; reaching its deltaLink
        stamps ``last_complete_at`` even when the backfill then runs on, so the later of
        the two stamps is when its new mail was last read."""
        if state is None or not state["delta_url"] or state["next_url"]:
            return False
        if state["status"] != "complete" and terminal_reason(state["reason"]):
            return False
        due = _stamp(self.store._clock() - timedelta(seconds=FRESH_SECONDS))
        return max(state["last_run_at"] or "", state["last_complete_at"] or "") < due

    def key(self, unit: tuple[str, str]) -> tuple[int, int]:
        """(band, mailbox rank) of a unit as its folder stands now."""
        mailbox_id, _folder = unit
        state = self.store.folder_state(*unit)
        rank = self.rank.get(mailbox_id, len(self.rank))
        if state is not None and terminal_reason(state["reason"]) \
                and state["status"] != "complete":
            return LAST, rank
        folder_id = state["folder_id"] if state is not None else None
        complete = state is not None and state["status"] == "complete"
        if self._delta_due(state):
            return FRESH_DUE, rank
        if (mailbox_id, folder_id) in self.bulk:
            return BULK, rank
        if complete:
            return FRESH, rank
        first = mailbox_id in self.first
        if self._recent(state, folder_id):
            return (FIRST_RECENT if first else OTHER_RECENT), rank
        return (FIRST_HISTORY if first else OTHER_HISTORY), rank
