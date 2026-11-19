"""The people a person works with and the conversations they have going, read from their
own mailbox (``assist.capture_history``: their mailbox only, sent and received), for the
Memory page: what the Workbench knows of who they deal with, beside what it remembers.

* **People** (``people_of``): every address they wrote to or heard from in the last
  ``DAYS`` days, ranked by how much they wrote to it (a message they sent counts
  ``SENT_WEIGHT``, one received counts one), newest contact breaking ties; with the
  counts, the last contact's day and subject, and whether the address is inside the
  company. Automated senders (no-reply, notifications, mailer daemons), mailings
  (``inbox_triage.bulk_senders``: bulk mail from an address they never wrote to) and anyone
  outside the company they never wrote to (a service's codes, bills and invitations) are
  no one.
* **Ongoing threads** (``threads_of``): a subject (``Re:``/``Fwd:`` taken off) with mail
  both ways in the last ``THREAD_DAYS`` days, newest first, saying whose turn it is: the
  last message came to them (``yours``: they owe the reply) or went from them
  (``theirs``: waiting on the other side).

**The people exclusion fails closed**: unreadable, or the reader themself not cleared,
nothing is returned; an address the directory maps to an excluded person (or that the
exclusion cannot decide) is left out of both lists, and a thread with such a person on it
is left out whole, as is every mail with such a person on it. A subject that carries pay or finance is never shown (``finance``).
Nothing is stored: every read computes from the mailbox as it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable, Mapping, Optional, Sequence

DAYS = 90
THREAD_DAYS = 14
SENT_WEIGHT = 2
MOST_PEOPLE = 15
MOST_THREADS = 12
_AUTOMATED = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|mailer[-_.]?daemon|postmaster|"
    r"alerts?|bounces?|automated|system|calendar|news(?:letter)?)\b|noreply", re.I)
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|tr)\s*(?:\[\d+\])?\s*:\s*)+", re.I)


def _when(text: Any) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def topic(subject: Any) -> str:
    """A subject as its conversation: reply and forward prefixes off, spaces folded."""
    return " ".join(_PREFIX.sub("", str(subject or "")).split())


def _guard(people: Any, me: str) -> Optional[Callable[[str], bool]]:
    """``address -> cleared`` for this reader, or None when the exclusion cannot be read or
    the reader is not cleared themself (then nothing at all is shown)."""
    try:
        if people is None or getattr(people, "unavailable", True) \
                or not getattr(people, "directory", None) or not people.identity_clear(me):
            return None
        directory = {str(a).lower(): str(o).lower() for a, o in people.directory.items()}
    except Exception:  # noqa: BLE001 - an exclusion that fails is a closed one
        return None

    def clear(address: str) -> bool:
        address = address.lower()
        try:
            return bool(people.identity_clear(directory.get(address, address)))
        except Exception:  # noqa: BLE001
            return False
    return clear


def _inside(people: Any) -> Callable[[str], bool]:
    internal = tuple(str(d).lower() for d in getattr(people, "internal", ()) or ())

    def inside(address: str) -> bool:
        domain = address.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in internal)
    return inside


def _others(row: Mapping[str, Any], own: set[str]) -> list[str]:
    if row.get("in"):
        found = [str(row.get("from") or "").lower()]
    else:
        found = [str(a).lower() for a in list(row.get("to") or ()) + list(row.get("cc") or ())]
    return [a for a in dict.fromkeys(found) if a and "@" in a and a not in own]


def automated(address: str) -> bool:
    return bool(_AUTOMATED.search(address.split("@", 1)[0]))


def people_and_threads(people: Any, me: str, rows: Sequence[Mapping[str, Any]], *,
                       now: datetime, finance: Callable[[str], Any] = lambda _t: False,
                       most_people: int = MOST_PEOPLE, most_threads: int = MOST_THREADS
                       ) -> Optional[dict[str, list[dict[str, Any]]]]:
    """``{"people": [...], "threads": [...]}`` of the reader ``me`` (their object id) from
    their own mail ``rows`` (``assist.sent_rows`` shape, received ones marked ``in``); None
    when the people exclusion cannot clear (module doc)."""
    clear = _guard(people, me)
    if clear is None:
        return None
    inside = _inside(people)
    own = {str(a).lower() for a, o in people.directory.items() if str(o).lower() == me}
    since, recent = now - timedelta(days=DAYS), now - timedelta(days=THREAD_DAYS)
    who: dict[str, dict[str, Any]] = {}
    talks: dict[str, dict[str, Any]] = {}
    barred: set[str] = set()
    # mailings (a newsletter, a webinar invite) are nobody the person works with
    from .inbox_triage import bulk_senders
    mass = bulk_senders(rows)
    for row in rows:
        at = _when(row.get("at"))
        if at is None or at < since or at > now + timedelta(minutes=5):
            continue
        others = [a for a in _others(row, own) if not automated(a) and a not in mass]
        if not others:
            continue
        # everyone on the mail is checked, the To and Cc of one received too
        on = {str(x).lower() for x in list(row.get("to") or ()) + list(row.get("cc") or ())
              if "@" in str(x)} - own
        blocked = [a for a in dict.fromkeys(others + sorted(on)) if not clear(a)]
        barred.update(blocked)
        subject = str(row.get("subject") or "")
        shown = "" if finance(subject) else subject[:200]
        # a mail with someone excluded on it tells nothing of anyone on it
        for address in others if not blocked else ():
            p = who.setdefault(address, {"address": address, "inside": inside(address),
                                         "sent": 0, "received": 0, "last_at": None,
                                         "last_subject": ""})
            p["received" if row.get("in") else "sent"] += 1
            if p["last_at"] is None or at > p["last_at"]:
                p["last_at"], p["last_subject"] = at, shown
        name = topic(subject)
        if at < recent or not name:
            continue
        t = talks.setdefault(name.lower(), {"subject": name[:200], "with": set(), "in": 0,
                                           "out": 0, "last_at": None, "last_in": False,
                                           "money": False})
        t["with"].update(others)
        t["money"] = t["money"] or bool(finance(subject))
        t["in" if row.get("in") else "out"] += 1
        if t["last_at"] is None or at > t["last_at"]:
            t["last_at"], t["last_in"] = at, bool(row.get("in"))
    # someone outside the company they never wrote to is a service (a sign-up code, a bill,
    # an invitation), not someone they work with; a colleague counts either way
    ranked = sorted((p for p in who.values() if p["sent"] or p["inside"]),
                    key=lambda p: (-(p["sent"] * SENT_WEIGHT + p["received"]),
                                   -p["last_at"].timestamp(), p["address"]))
    out_people = [{**p, "last_at": p["last_at"].isoformat()} for p in ranked[:most_people]]
    out_threads = []
    for t in sorted(talks.values(), key=lambda t: -t["last_at"].timestamp()):
        if not t["in"] or not t["out"] or t["money"] or t["with"] & barred:
            continue   # one-way mail is no conversation; pay/finance or an excluded person
        out_threads.append({"subject": t["subject"], "others": sorted(t["with"])[:6],
                            "messages": t["in"] + t["out"], "last_at": t["last_at"].isoformat(),
                            "turn": "yours" if t["last_in"] else "theirs"})
        if len(out_threads) >= most_threads:
            break
    return {"people": out_people, "threads": out_threads}


def label(address: str) -> str:
    """A readable name from an address's own parts ("ana.lopez@x" -> "Ana Lopez")."""
    local = address.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-+]+", local) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts) or address
