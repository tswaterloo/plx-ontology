"""The reading side of the autopilot: visible items, handles, digests, cards and the store
(``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence
from .citation_schemes import HANDLE_PREFIX

from .autopilot_records import _iso
from .autopilot_queue import CARD_PREFIX, Queue


# ----------------------------------------------------------------------------- reading side

def visible(queue: Queue, *, reader: str, may_read: Callable[[str], bool],
            held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
            grants: Mapping[str, Any] | None = None,
            states: Sequence[str] = ("open",), also: Iterable[str] = ()
            ) -> list[dict[str, Any]]:
    """The items this reader may see (module doc), fail closed; ``also``: the ids of
    items passed to them (not theirs: ``mine`` false, ``handed`` true), under the same
    checks of their own."""
    from .proactive_commitments import owner_ids
    mine = owner_ids(reader)
    if not mine:
        return []
    grants = grants or {}
    teams = grants.get("teams") or {}
    held_teams = set()
    for oid in mine:
        held_teams.update((grants.get("managers") or {}).get(oid) or ())
    reports = {o for o, t in teams.items() if t in held_teams}
    owners = mine | reports
    held = set(held)

    def readable(item: Mapping[str, Any]) -> bool:
        try:
            if not all(may_read(a) for a in item["audiences"]):
                return False
            if not set(item["markings"]) <= held:
                return False
            cited = item_handles(item)
            if cited:
                refused = decide(cited)
                # every handle decided readable, or the item is not shown (fail closed)
                if not isinstance(refused, Mapping) or \
                        any(h not in refused or refused[h] is not None for h in cited):
                    return False
        except Exception:  # noqa: BLE001 - an undecidable item is not shown
            return False
        return True
    out = []
    for item in queue.items(owners, states):
        if readable(item):
            item["mine"] = item["owner"] in mine
            out.append(item)
    seen = {i["item_id"] for i in out}
    for item_id in dict.fromkeys(also):
        item = None if item_id in seen else queue.item(str(item_id))
        if item is not None and item["state"] in states and readable(item):
            item.update(mine=False, handed=True)
            out.append(item)
    return out


_CITED = re.compile(HANDLE_PREFIX)


def item_handles(item: Mapping[str, Any]) -> list[str]:
    """Every citation of an item: its ``citations`` and any handle its prepared work or
    event names (an item stored before the list was whole kept only its first 40)."""
    out = list(item.get("citations") or ())

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if _CITED.match(value):
                out.append(value)
        elif isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
    walk(item.get("body") or {})
    return list(dict.fromkeys(out))


def digest(items: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """A person's daily digest: counts per autopilot, items due today, nudges for items
    past due (oldest first)."""
    stamp = _iso(now)
    by = Counter(i["autopilot"] for i in items)
    late = sorted((i for i in items if i["due_at"] < stamp), key=lambda i: i["due_at"])
    today = [i for i in items if i["due_at"][:10] == stamp[:10]]
    from . import autopilot_work as aw
    from . import mc_digest as md
    return {"open": len(items), "by_autopilot": dict(sorted(by.items())),
            "due_today": len(today), "brief": aw.brief(items, now),
            "mc_approvals": md.digest(items, now),
            "nudges": [{"item_id": i["item_id"], "title": i["title"], "due_at": i["due_at"],
                        "text": f"Waiting since {i['created_at'][:16].replace('T', ' ')} UTC: "
                                f"{i['title']}"} for i in late[:20]]}


def card_of(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The item's drafted message as a card ``card_actions.ActionDesk`` accepts (sent only
    through its preview and the owner's confirm); None without an email draft."""
    work = (item.get("body") or {}).get("prepared") or {}
    draft = work.get("draft") or {}
    to = [t.get("address") for t in draft.get("to") or () if t.get("address")]
    cites = [c for c in item.get("citations") or () if isinstance(c, str)]
    if draft.get("channel") not in ("email", "mail") or not to or not cites:
        return None
    return {"id": CARD_PREFIX + item["item_id"], "headline": str(item.get("title"))[:300],
            "subject": {"label": str(draft.get("subject") or item.get("title"))[:200]},
            "action": {"text": ""},
            "draft": {"channel": "email", "to": {"address": to[0], "source": cites[0]},
                      # a reply's other recipients, as the person answers (``reply_all``):
                      # each is checked again like any recipient when the card is previewed
                      "also_to": to[1:], "cc": [t.get("address") for t in draft.get("cc") or ()
                                                if isinstance(t, Mapping) and t.get("address")],
                      "subject": draft.get("subject"), "body": draft.get("body"), "facts": []},
            "teams_draft": None, "playbook": True}


def write_spec(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The item's dry-run write intent spec for ``WriteDesk.prepare`` (by the autopilot)."""
    work = (item.get("body") or {}).get("prepared") or {}
    spec = work.get("write")
    if not isinstance(spec, Mapping) or not item.get("citations"):
        return None
    return {**spec, "citations": list(item["citations"])[:20]}


def _plain(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: dict(v) if isinstance(v, Counter) else v for k, v in summary.items()}


# ----------------------------------------------------------------------------- store

# Which store a replay or score reads (``_store``): the SQLite generation file alone, or the
# Postgres ontology the API reads in mode 1 (current to the last delta layer applied).
STORE_SOURCES = ("sqlite", "postgres")


def _store(path: str, source: str | None = None) -> Any:
    """The store at ``path`` (``proactive_engine._Store``, its reads through
    ``proactive_engine._connect``). ``source``: ``"postgres"`` turns on the API's mode 1
    (``PLX_ONTOLOGY_PG=1``) in this process, so every read runs on Postgres through the
    compat layer (``operating.pg.sqlite_compat``) when Postgres holds the generation at
    ``path``; ``"sqlite"`` turns it off, so only the file is read; None: as the environment
    says. The store opened must be the one asked for: Postgres not serving that
    generation (not loaded, a load in progress, unreachable) is ``store_source_mismatch``,
    never a quiet read of the older SQLite file."""
    from ontology import store as ostore
    from . import proactive_engine as pe
    from .pg import ontology_reader
    if source is not None and source not in STORE_SOURCES:
        raise ValueError(f"unknown store source {source!r}")
    if source == "postgres" and not ontology_reader.enabled():
        os.environ[ontology_reader.ENV] = "1"
    elif source == "sqlite":
        if ontology_reader.pg_only():
            raise ostore.StoreError("store_source_mismatch", "postgres_only")
        os.environ.pop(ontology_reader.ENV, None)
    st = pe._Store(Path(path), threads=4)
    if source is not None and store_source(st)["store"] != source:
        st.close()
        raise ostore.StoreError("store_source_mismatch", f"{source}_not_served")
    return st


def store_source(st: Any) -> dict[str, Any]:
    """Which store ``st`` reads and what it held when opened (a SQLite generation's content
    digest; Postgres: its generation's digest and data version), for a result's
    provenance."""
    return {"store": "sqlite" if isinstance(st.conn, sqlite3.Connection) else "postgres",
            "stamp": st.digest}
