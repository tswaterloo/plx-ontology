"""``plx.my_open_work``: the Ask's read of the person's OWN built-up store of work, in place
of reading mail from scratch (owner 11-24, W2).

What it holds, all as the person's own door shows it:

* ``matters``: Assist's open and unverified matters (``assist.matter_items``), ALL ages (the
  Assist list's 14-day window is not applied: Guardrail 1), each with its kind (approval,
  task from Planner or Mission Control, conversation read from a mail or Teams message,
  wait, routine, alert), the channel it came from (mail or Teams), who from, the quote the
  reading rests on, the next act, the due time, whether a draft or a decision for the person
  is prepared, and whether it has gone quiet. The matters cycle builds them continuously from
  the readings of every message (mail and Teams), the waits and promises, the Planner and
  Mission Control tasks, so they cover weeks of context.
* ``notes``: what the person told the Workbench themselves (memory items, kind fact,
  commitment, instruction, preference), those about a record first: private to them.

Paged by size, never cut: ``next_offset`` continues the matters. A store that is off for the
person (no opt-in), stale or unreadable says so in ``status`` and ``reason``, never an empty
list in its place. The owner may name another ``person`` (their own matters, as the owner's
list shows every person's); anyone else is refused.

The calendar is the ``meetings`` view (plx.sql); this tool does not read it.
Evidence: isolated-fixture (``operator_api/tests/test_open_work_tool.py``)."""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

LOG = logging.getLogger(__name__)
NAME = "plx.my_open_work"
# one page's size in characters (the model's context is the only bound: the next page is
# one call away)
PAGE_CHARS = 30_000

SPEC = {
    "name": NAME,
    "description": (
        "The person's own built-up store of open work, kept up to date continuously from "
        "every mail and Teams message they were on, their approvals, Planner and Mission "
        "Control tasks, what they wait for and what they promised: for 'what do I have to "
        "do', 'what is open', 'what am I waiting on', 'what did I promise'. Read it FIRST "
        "for such questions, before any mail search. matters: every open matter of any age "
        "with kind, channel (mail, teams, system), from, subject, the quote it rests on, "
        "next act, due, whether a draft is prepared, the ref of the message it rests on "
        "(plx.open reads it; the answer's list links it), and 'still_open' when it has gone "
        "quiet since it was due; notes: what the person told the Workbench themselves "
        "(about meetings, customers, 'already handled', this week's priorities), which "
        "override the matters they contradict. status other than ok or stale says the "
        "store is not available to them: say so and read their mail and Teams with "
        "plx.search instead. next_offset continues the list. person: the owner only, "
        "another person's matters. The calendar is the meetings view of plx.sql."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "kind": {"type": "string", "maxLength": 40},
            "person": {"type": "string", "maxLength": 200}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _row(i: Mapping[str, Any]) -> dict[str, Any]:
    out = {"id": i["item_id"], "kind": i["kind_of_matter"], "state": i["state"],
           "channel": i["channel"], "from": i["sender"], "subject": i["subject"],
           "summary": i["summary"], "quote": i["message"], "note": i["note"],
           "next_act": i["ask"] or None, "basis_at": _iso(i["basis_at"]),
           "due_at": _iso(i["due_at"]), "still_open": bool(i["still_open"]),
           "draft_prepared": bool(i["body"])}
    # the message or record the matter rests on: its handle and the short ref the answer's
    # items block links by (a handle too short for a ref is written as it is)
    handle = next(iter(i.get("citations") or ()), None)
    if handle:
        from operating import open_refs
        out["open"] = handle
        if len(handle) >= open_refs.MIN_CHARS and handle.split("://", 1)[0] in open_refs.SCHEMES:
            out["ref"] = open_refs.short(handle)
    return {k: v for k, v in out.items() if v not in (None, "", False)} | {
        "id": out["id"], "kind": out["kind"], "state": out["state"]}


def _notes(backend: Any, reader: Any) -> Any:
    """The person's own memory items (all of them) as notes; None when memory cannot be read."""
    store = getattr(backend, "memory", None)
    if store is None:
        return None
    decide = getattr(backend, "_memory_decide", None)
    try:
        rows = store.items(reader, limit=None,
                           decide=decide(reader, wait=True) if callable(decide) else None)
    except Exception:  # noqa: BLE001 - said in the result, never an empty list
        LOG.warning("open work: notes unreadable", exc_info=True)
        return None
    return [{"id": r["id"], "kind": r["kind"], "at": r["at"], "text": r["text"],
             **({"about": r["records"]} if r.get("records") else {})}
            for r in sorted(rows, key=lambda r: (not r.get("records"), ))]


def store(backend: Any, reader: Any, *, person: str = "") -> dict[str, Any]:
    """The person's own Assist store in one read, all ages, as ``call`` uses it: the raw
    matter rows and the store's honest status (never an empty list for off or unreadable)."""
    from operating import assist_matters as am
    from operating import autopilot_engine as ae
    from . import assist_routes as routes
    pool = routes._pool(backend)
    if pool is None:
        return {"status": "not_connected", "items": [],
                "reason": "the Assist store is not connected here: read mail and Teams "
                          "with plx.search"}
    owner = bool(backend._apa_is_owner(reader))
    person = person.strip().lower()
    if person and not owner:
        return {"status": "refused", "items": [],
                "reason": "only the owner reads another person's matters"}
    ids = {person} if person else routes._ids(backend, reader)
    pools = set() if person else set(backend._apa_pools(reader))
    from operating.card_actions import JournalBroken
    try:
        rows = backend._apa_rows(backend._apa_state())
    except (OSError, ValueError, JournalBroken):
        LOG.warning("open work: the opt-in journal could not be read", exc_info=True)
        rows = None
    shown = am.shown_to(ids, rows=rows, draft=ae.load_draft())
    with pool.connection() as pg:
        return am.as_items(pg, ids | pools, shown=shown, owner_view=owner, everyone=False,
                           all_ages=True, door=routes._door(backend, reader),
                           names_of=routes._names_of(backend))


def call(backend: Any, reader: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    person = str(args.get("person") or "").strip().lower()
    got = store(backend, reader, person=person)
    if got["status"] in ("not_connected", "refused"):
        return got
    items = [i for i in got["items"] if not args.get("kind") or i["kind_of_matter"] == args["kind"]]
    start = args.get("offset") if type(args.get("offset")) is int else 0
    page, size, at = [], 0, start
    for i in items[start:]:
        row = _row(i)
        size += len(json.dumps(row, default=str))
        if page and size > PAGE_CHARS:
            break
        page.append(row)
        at += 1
    out: dict[str, Any] = {
        "status": got["status"], "reason": got["reason"],
        "as_of": _iso(got.get("as_of")), "matters_total": len(items),
        "matters_shown_from": start, "matters": page,
        "next_offset": at if at < len(items) else None,
        "read": "Assist matters (all ages) built from the readings of every mail and Teams "
                "message, approvals, Planner and Mission Control tasks, waits and promises"}
    if got.get("older"):
        out["older_not_shown"] = got["older"]
    if start == 0 and not person:
        notes = _notes(backend, reader)
        out["notes"] = notes if notes is not None else []
        if notes is None:
            out["notes_status"] = "unreadable: the person's notes could not be read now"
    return out
