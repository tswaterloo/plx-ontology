"""Who does a role's work, derived from what people did (never a list of names): the
autopilot's role owners (``{"roles": {role: [oid]}}``, the file ``autopilot._roles`` reads).

A role is a kind of record (``RULES``: purchasing -> the purchase orders, from BC, the
Portal, FileMaker or Sage). Over a window, each person scores on the distinct records of
that kind they acted on:

* **sent** (weight 1): mail the person sent to an outside address that names one of the
  records (the store's ``Email.mentions_*`` link), each message counted once however many
  mailboxes hold a copy;
* **addressed** (weight 1/2): outside mail naming one of the records sent To the person;
* **system** (weight 1): a record action the store attributes to the person (an event
  object with qualifier ``actor`` naming the person's id: the per-person system activity
  lands there; none yet).

A person is in the role with at least ``min_records`` records scored and at least
``min_share`` of the role's top score. Every person passes the people exclusion first
(unavailable: ``blocked``, nothing derived; an excluded or undecidable person never
appears, and only their count is reported). The output holds ids and counts, no names,
addresses or message text. Portal user rows that flag a PO contact but carry no address
are counted (``portal_flags_unresolved``) and never joined by guess.

Run (read-only on the store; writes only ``--out``)::

    python3 -m operating.autopilot_roster --store STORE --days 90 --out roles.json
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from . import autopilot as ap

RULES = {
    "purchasing": {"record": "PurchaseOrder", "link": "Email.mentions_purchase_order",
                   "portal_flag": ("RelPublicAppUser", "is_po_contact")},
}
CHUNK = 2000
MAX_MEMBERS = 15


def _window_mail(st: Any, since: str, until: str) -> list[str]:
    return sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM events e JOIN event_objects eo ON eo.event_id = "
        "e.event_id WHERE e.event_type = 'Email.received' AND e.time >= ? AND e.time < ?",
        (since, until)) if r[0].startswith("Email:")})


def _actors(st: Any, record: str, since: str, until: str) -> dict[str, set[str]]:
    """person id -> records of the kind an event names them the actor of."""
    out: dict[str, set[str]] = defaultdict(set)
    rows = st.conn.execute(
        "SELECT a.object_id, r.object_id FROM events e JOIN event_objects a ON a.event_id = "
        "e.event_id AND a.qualifier = 'actor' JOIN event_objects r ON r.event_id = e.event_id "
        "AND r.object_id LIKE ? WHERE e.time >= ? AND e.time < ?",
        (record + ":%", since, until))
    for actor, rec in rows:
        out[str(actor).rsplit(":", 1)[-1].lower()].add(rec)
    return out


def derive(st: Any, people: Any, role: str, since: str, until: str, *,
           min_records: int = 3, min_share: float = 0.1) -> dict[str, Any]:
    if role not in RULES:
        raise KeyError(role)
    if people is None or getattr(people, "unavailable", None):
        return {"status": "blocked", "reason": "people_exclusion_unavailable", "members": []}
    rule = RULES[role]
    directory, addresses_of, domains = ap._people_maps(people)
    sent: dict[str, set[str]] = defaultdict(set)
    addressed: dict[str, set[str]] = defaultdict(set)
    systems: dict[str, Counter] = defaultdict(Counter)
    seen_messages: set[tuple[str, str]] = set()
    mails = _window_mail(st, since, until)
    for start in range(0, len(mails), CHUNK):
        chunk = mails[start:start + CHUNK]
        named: dict[str, set[str]] = defaultdict(set)
        for row in st.touching(chunk, (rule["link"],)):
            a, b = row[3], row[4]
            if a.startswith("Email:") and b.startswith(rule["record"] + ":"):
                named[a].add(b)
        rows = ap._read(st, sorted(named), ("sender", "to_addresses", "internet_message_id"))
        for oid, recs in named.items():
            r = rows.get(oid) or {}
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            to = ap._addresses(ap._v(r, "to_addresses"))
            message = ap._v(r, "internet_message_id") or oid
            if not sender:
                continue
            if ap._internal(sender, domains):
                who = directory.get(sender)
                if who and any(not ap._internal(t, domains) for t in to):
                    for rec in recs:
                        if (message, rec) not in seen_messages:
                            seen_messages.add((message, rec))
                            sent[who].add(rec)
                            systems[who][rec.split(":", 2)[1].split(".", 1)[0]] += 1
            else:
                for t in to:
                    who = directory.get(t)
                    if who:
                        addressed[who].update(recs)
    actors = _actors(st, rule["record"], since, until)
    scores: dict[str, float] = {}
    for who in set(sent) | set(addressed) | set(actors):
        own = sent.get(who, set()) | actors.get(who, set())
        scores[who] = len(own) + 0.5 * len(addressed.get(who, set()) - own)
    excluded = sum(1 for who in scores if not people.identity_clear(who))
    scores = {w: s for w, s in scores.items() if people.identity_clear(w)}
    top = max(scores.values(), default=0.0)
    members = sorted((w for w, s in scores.items()
                      if s >= min_records and top and s / top >= min_share),
                     key=lambda w: (-scores[w], w))[:MAX_MEMBERS]
    flags = 0
    otype, prop = rule["portal_flag"]
    ids = [r[0] for r in st.conn.execute("SELECT object_id FROM objects WHERE object_type = ?",
                                         (otype,))]
    for row in ap._read(st, ids, (prop,)).values():
        flags += ap._v(row, prop).lower() in ("true", "1")
    return {"status": "complete", "role": role, "window": [since, until],
            "members": members,
            "evidence": {w: {"score": scores[w], "sent": len(sent.get(w, ())),
                             "addressed": len(addressed.get(w, ())),
                             "system": len(actors.get(w, ())),
                             "systems": dict(systems.get(w, {}))} for w in members},
            "candidates": len(scores), "excluded_dropped": excluded,
            "portal_flags_unresolved": flags}


def merge_roles(path: Path, role: str, members: Sequence[str]) -> dict[str, Any]:
    """The roles file with ``role`` replaced by ``members`` (other roles kept)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    roles = dict(raw.get("roles") or {}) if isinstance(raw, Mapping) else {}
    roles[role] = list(members)
    return {"roles": roles}


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_roster")
    p.add_argument("--store", required=True)
    p.add_argument("--role", default="purchasing", choices=sorted(RULES))
    p.add_argument("--days", type=float, default=90.0)
    p.add_argument("--until", default=None)
    p.add_argument("--out", required=True, help="roles file to write (other roles kept)")
    a = p.parse_args(argv)
    until = a.until or ap._iso(datetime.now(timezone.utc))
    since = ap._iso(ap._instant(until) - timedelta(days=a.days))
    people = pe._people(Path(a.store))
    st = ap._store(a.store)
    try:
        found = derive(st, people, a.role, since, until)
    finally:
        st.close()
    if found["status"] == "complete":
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(merge_roles(out, a.role, found["members"]), indent=1))
        os.replace(tmp, out)
    print(json.dumps({k: v for k, v in found.items() if k != "evidence"}
                     | {"evidence": list(found.get("evidence", {}).values())}, indent=1))
    return 0 if found["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
