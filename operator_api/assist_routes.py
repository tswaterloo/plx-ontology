"""Assist's routes (ASSIST specs 2.3): the person's list read from matters
(``operating.assist_matters``), and the item actions on a matter. ``app.py`` is over its size
cap, so the routes live here and ``install`` registers them before the queue's own item
routes: a matter id (24 hex characters, the ``matter`` path convertor) is answered here, any
other item id (a queue item, ``ap_...``) falls through to the queue's routes unchanged.

* ``GET /api/autopilot/drafts``: the reader's open and unverified matters (``as_items``: 14
  days by basis, ``older`` counts the rest, ``all_ages=true`` lists them all), each through
  the reader's door (audiences, markings, every cited handle, decided once for the list);
  ``status`` off when they are not shown (no opt-in), stale when the matters cycle stopped.
  The owner reads every person's, marked "not shown to staff".
* ``POST /api/autopilot/items/{id}/draft``: draft on request (a ``requested`` log row; the
  drafting worker serves requests first) -> ``{"status": "working"}``.
* ``decision`` / ``reply-from-line`` / ``hand`` / ``take`` on a matter: a matter made from a
  queue item acts on that item through the queue's own handler (its ``legacy_item_id``); any
  other matter takes the person's row in the log (``closed`` with the decision, or
  ``person_line``), which the next cycle applies. Hand and take of a matter that came from
  no queue item are refused, said.

Nothing here sends or writes anywhere but ``assist.matter_log`` (append only: the API's role
may do nothing else to the Assist tables)."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Optional, Sequence

from fastapi import Depends
from starlette.convertors import Convertor, register_url_convertor

from . import models as m
from .auth import Reader
from .errors import UserFacing

LOG = logging.getLogger(__name__)


class MatterId(Convertor):
    regex = "[0-9a-f]{24}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("matter", MatterId())

_ERR = {401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
        409: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}}


def _pool(backend: Any) -> Any:
    return getattr(getattr(backend, "memory", None), "pool", None)


def _door(backend: Any, reader: Reader) -> Callable[[Sequence[Mapping[str, Any]]], set]:
    """The reader's door over a whole list at once: the grant read once, every cited handle
    of every item decided in one call (``autopilot_reading.may_show`` per item)."""
    from operating.autopilot_reading import may_show

    def decide_all(may_read: Any, held: Any, decide: Any, items: Sequence[Mapping]) -> set:
        cited = sorted({h for i in items for h in i["citations"]})
        try:
            refused = decide(cited) if cited else {}
        except Exception:  # noqa: BLE001 - undecidable: nothing is shown, said in the log
            LOG.warning("assist list: the door could not decide %d handles", len(cited))
            return set()
        held = set(held)
        return {i["item_id"] for i in items if may_show(
            i, may_read=may_read, held=held, refused=refused, cited=i["citations"])}

    def door(items: Sequence[Mapping[str, Any]]) -> set:
        grant = getattr(backend, "autopilot_grant", None)
        if grant is not None:
            may_read, held, decide = grant
            return decide_all(may_read, held, decide, items)

        def job(view: Any) -> set:
            access = getattr(view, "access", None)
            if access is None or not hasattr(access, "grant"):
                return set()           # no grant to check: nothing is shown
            with view._request():
                may_read, held = access.grant()
                return decide_all(may_read, held, access.decide, items)
        return backend.reads.run(reader, job, timeout=60)
    return door


def _ids(backend: Any, reader: Reader) -> set[str]:
    return set(backend._apa_me(reader)) - {"unknown"}


def _names_of(backend: Any) -> Optional[Callable[[Any], Mapping[str, str]]]:
    viewing = getattr(backend, "view_as", None)
    return viewing.names_of if viewing is not None and getattr(viewing, "available", False) else None


def matters_list(backend: Any, reader: Reader, *, all_ages: bool = False,
                 fresh: bool = False) -> m.AutopilotDrafts:
    """The reader's matters as ``AutopilotDrafts`` (module text); ``fresh``: the queue's
    cards read now, not from the kept list (an answer to a write)."""
    from operating import assist_matters as am
    from operating import autopilot_engine as ae
    pool = _pool(backend)
    read_queue = backend._prepared_read if fresh else backend.autopilot_prepared_list
    if pool is None:      # no matters here: the queue's own cards only, and it is said
        queue = read_queue(reader)
        return m.AutopilotDrafts(items=list(queue.items), never_senders=queue.never_senders,
                                 status="not_run", reason="the Assist store is not connected here")
    owner = bool(backend._apa_is_owner(reader))
    ids = _ids(backend, reader)
    pools = set(backend._apa_pools(reader))
    state = backend._apa_state()
    from operating.card_actions import JournalBroken
    try:
        rows = backend._apa_rows(state)
    except (OSError, ValueError, JournalBroken):
        LOG.warning("assist list: the opt-in journal could not be read", exc_info=True)
        rows = None             # unreadable: nobody is shown (F26), said as opt_in_unreadable
    shown = am.shown_to(ids, rows=rows, draft=ae.load_draft())
    with pool.connection() as pg:
        got = am.as_items(pg, ids | pools, shown=shown, owner_view=owner, everyone=owner,
                          all_ages=all_ages, door=_door(backend, reader),
                          names_of=_names_of(backend))
    never = backend._apa_never(state, ids)
    # a matter made from one of the remaining queue triggers' items is listed as that item's
    # own card (its prepared changes, holds and links), read as the queue lists it
    items = _put_off(backend, state, [_prepared(i) for i in got["items"]
                                      if not i["queue_card"] and i["kind_of_matter"] != am.MC_APPROVAL_KIND])
    queue = read_queue(reader).items
    items = sorted(items + list(queue), key=lambda i: i.created_at, reverse=True)
    return m.AutopilotDrafts(items=items, never_senders=never, older=got["older"],
                             status=got["status"], reason=got["reason"],
                             stale_since=got["as_of"].isoformat()
                             if got["status"] == "stale" and got.get("as_of") else None)


def _prepared(i: Mapping[str, Any]) -> m.AutopilotPrepared:
    from operating.assist_matters import clean_note_spacing
    def iso(v: Any) -> Optional[str]:
        return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)
    return m.AutopilotPrepared(
        item_id=i["item_id"], sender=i["sender"], channel=i["channel"], subject=i["subject"],
        message=i["message"], kind=i["kind"], summary=i["summary"],
        note=clean_note_spacing(i["note"]),
        body=i["body"], to=i["to"], ask=i.get("ask", ""), created_at=iso(i["basis_at"]) or "",
        state=i["state"], verified_at=iso(i["verified_at"]), plan_status=i["plan_status"],
        plan_reason=i["plan_reason"], plan_at=iso(i.get("plan_at")),
        requested_at=iso(i.get("requested_at")))


def _matter(backend: Any, reader: Reader, item_id: str) -> dict[str, Any]:
    """The reader's own listed matter (as the list shows it, through the door), else 404."""
    got = matters_list(backend, reader, all_ages=True)
    if not any(i.item_id == item_id for i in got.items):
        raise UserFacing("not_found", "That item is not on your list.", 404)
    pool = _pool(backend)
    with pool.connection() as pg:
        row = pg.execute("SELECT legacy_item_id, owner_person FROM assist.matter_items WHERE "
                         "matter_id = %s", (item_id,)).fetchone()
    if row is None:
        raise UserFacing("not_found", "That item is not on your list.", 404)
    return {"legacy": row[0], "owner": row[1]}


def _log(backend: Any, item_id: str, what: str, detail: Mapping[str, Any]) -> None:
    with _pool(backend).connection() as pg:
        pg.execute("INSERT INTO assist.matter_log (matter_id, what, detail) VALUES (%s, %s, %s)",
                   (item_id, what, json.dumps(dict(detail))))
        pg.commit()           # the pool's hand-back rolls an open transaction back


def install(app: Any, backend: Any, reader: Callable[..., Reader], notes: Any) -> None:
    """Register the routes (before the queue's item routes: see the module text)."""

    @app.get("/api/autopilot/drafts", response_model=m.AutopilotDrafts, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def assist_list(all_ages: bool = False, who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        """Your open work, newest first: each item kept true against its source; the last
        14 days unless ``all_ages`` (``older`` says how many more)."""
        return matters_list(backend, who, all_ages=all_ages)

    @app.post("/api/autopilot/items/{item_id:matter}/draft", response_model=m.AssistWorking,
              tags=["actions"], responses=_ERR)
    def assist_draft(item_id: str, who: Reader = Depends(reader)) -> m.AssistWorking:
        """Draft this item now (it goes to the front of the drafting queue)."""
        _matter(backend, who, item_id)
        _log(backend, item_id, "requested", {"by": sorted(_ids(backend, who))[:1]})
        return m.AssistWorking(status="working")

    @app.post("/api/autopilot/items/{item_id:matter}/decision", response_model=m.AutopilotItem,
              tags=["actions"], responses=_ERR)
    def assist_decide(item_id: str, body: m.AutopilotDecisionRequest,
                      who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Done, not needed (reject) or snooze on an item of yours. Nothing is sent."""
        found = _matter(backend, who, item_id)
        if found["legacy"]:
            item = backend.autopilot_decide(who, found["legacy"], body)
            notes.autopilot_decided(who, item)
            return item
        if body.decision not in ("accept", "reject", "snooze"):
            raise UserFacing("action_refused", "This item can be marked done, not needed or "
                             "put off until later.", 409)
        _journal_decision(backend, who, item_id, body)
        if body.decision != "snooze":
            _log(backend, item_id, "closed", {"by": sorted(_ids(backend, who))[:1],
                                              "decision": body.decision, "note": body.note or ""})
        return _decided(item_id, found["owner"], body.decision)

    @app.post("/api/autopilot/items/{item_id:matter}/reply-from-line",
              response_model=m.AutopilotItem, tags=["actions"], responses=_ERR)
    def assist_line(item_id: str, body: m.AutopilotLineRequest,
                    who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Your one line for this item: the draft is written from it."""
        found = _matter(backend, who, item_id)
        if found["legacy"]:
            return backend.autopilot_reply_from_line(who, found["legacy"], body)
        _log(backend, item_id, "person_line", {"by": sorted(_ids(backend, who))[:1],
                                               "line": body.line})
        _log(backend, item_id, "requested", {"by": sorted(_ids(backend, who))[:1]})
        return _decided(item_id, found["owner"], None)

    @app.post("/api/autopilot/items/{item_id:matter}/hand", response_model=m.AutopilotDrafts,
              tags=["actions"], responses=_ERR)
    def assist_hand(item_id: str, body: m.AutopilotHandRequest,
                    who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        found = _matter(backend, who, item_id)
        if not found["legacy"]:
            raise UserFacing("action_refused", "This item cannot be passed on yet.", 409)
        backend.autopilot_hand(who, found["legacy"], body)
        return matters_list(backend, who, fresh=True)

    @app.post("/api/autopilot/items/{item_id:matter}/take", response_model=m.AutopilotDrafts,
              tags=["actions"], responses=_ERR)
    def assist_take(item_id: str, who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        found = _matter(backend, who, item_id)
        if not found["legacy"]:
            raise UserFacing("action_refused", "This item cannot be taken yet.", 409)
        backend.autopilot_take(who, found["legacy"])
        return matters_list(backend, who, fresh=True)


def _journal_decision(backend: Any, reader: Reader, item_id: str,
                      body: m.AutopilotDecisionRequest) -> None:
    """The person's word on a matter, in the decisions journal the queue's cards use, so the
    list stops showing it at once (a done or not-needed matter is closed by the next matters
    cycle; a Later one comes back when its time is up)."""
    from datetime import datetime, timedelta, timezone
    from .autopilot_area import DECISIONS_FILE, SNOOZE_DAYS, _instant, _iso
    from operating.card_actions import Journal
    state = backend._apa_state()
    if state is None:
        raise UserFacing("action_refused", "The autopilot's state directory is not configured "
                         "on this server.", 409)
    now = datetime.now(timezone.utc)
    row: dict[str, Any] = {"event": "item_decision", "item_id": item_id,
                           "decision": body.decision, "at": _iso(now),
                           "by": sorted(backend._apa_me(reader))[0],
                           "note": (body.note or "").strip() or None, "matter": True}
    if body.decision == "snooze":
        until = _instant(body.until)
        if until is None or until <= now or until > now + timedelta(days=SNOOZE_DAYS):
            raise UserFacing("bad_request", f"Later needs a time in the next {SNOOZE_DAYS} "
                             "days.", 422)
        row["until"] = _iso(until)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Journal(state / DECISIONS_FILE).locked() as held:
        held.append(row)


def _put_off(backend: Any, state: Any, items: Sequence[Mapping[str, Any]]) -> list:
    """The matters the person already closed or put off since their basis message (their word
    in the decisions journal), left out of the list until the cycle catches up."""
    from datetime import datetime, timezone
    from .autopilot_area import _instant
    if state is None:
        return list(items)
    said = backend._apa_decisions(state)
    now = datetime.now(timezone.utc)
    out = []
    for i in items:
        d = said.get(i.item_id)
        at = _instant(d.get("at")) if d and d.get("matter") else None
        basis = _instant(i.created_at)
        if at is not None and (basis is None or at > basis):
            if d["decision"] in ("accept", "reject"):
                continue
            until = _instant(d.get("until"))
            if d["decision"] == "snooze" and until is not None and until > now:
                continue
        out.append(i)
    return out


def _decided(item_id: str, owner: str, decision: Optional[str]) -> m.AutopilotItem:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return m.AutopilotItem(
        item_id=item_id, autopilot="assist", label="Assist", title="", workflow="assist",
        system="other", owner=owner, mine=True, due_at=now, created_at=now, today=True,
        state="accepted" if decision == "accept" else "rejected" if decision == "reject"
        else "waiting",
        decision=m.AutopilotDecision(decision=decision, at=now, by_you=True)
        if decision else None)
