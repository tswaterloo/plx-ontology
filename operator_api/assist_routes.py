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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import re
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


def _bump_assist_drafts(backend: Any) -> None:
    """Advance the in-process Assist drafts cache version so the next ``GET /api/autopilot/drafts``
    sees matter/queue writes immediately."""
    lock = getattr(backend, "_lock", None)
    if lock is not None:
        with lock:
            backend._assist_drafts_ver = int(getattr(backend, "_assist_drafts_ver", 0)) + 1
    else:
        backend._assist_drafts_ver = int(getattr(backend, "_assist_drafts_ver", 0)) + 1


def _door(backend: Any, reader: Reader) -> Callable[[Sequence[Mapping[str, Any]]], set]:
    """The reader's door over a whole list at once: the grant read once, items pre-filtered
    by audience and marking before deciding cited handles in one call
    (``autopilot_reading.may_show`` per item; fail closed)."""
    from operating.autopilot_reading import may_show

    def decide_all(may_read: Any, held: Any, decide: Any, items: Sequence[Mapping]) -> set:
        held_set = set(held)
        eligible: list[Mapping[str, Any]] = []
        for i in items:
            try:
                if all(may_read(a) for a in i["audiences"]) and set(i["markings"]) <= held_set:
                    eligible.append(i)
            except Exception:  # noqa: BLE001 - undecidable item is not shown (fail closed)
                continue
        if not eligible:
            return set()
        cited = sorted({h for i in eligible for h in i["citations"]})
        try:
            refused = decide(cited) if cited else {}
        except Exception:  # noqa: BLE001 - undecidable: nothing is shown, said in the log
            LOG.warning("assist list: the door could not decide %d handles", len(cited))
            return set()
        return {i["item_id"] for i in eligible if may_show(
            i, may_read=may_read, held=held_set, refused=refused, cited=i["citations"])}

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


_MC_APPROVAL_AUTOPILOTS = frozenset({"job_approval", "approval_request", "mc_approval_pending"})
_MC_APPROVAL_SPAM_RE = re.compile(
    r"(?:\bmerge\s+approval\s+for\b"
    r"|\bstage\s+task-\d+\b"
    r"|\bvia\s+chief\s+of\s+staff\s*:\s*get\s+everything\s+merged\b"
    r"|\bget\s+everything\s+merged\b"
    r"|\bmerge[- ]gate\b"
    r"|\bpr\s+merge[- ]class\b"
    r"|\bautomated\s+ci\s+approval\b)",
    re.IGNORECASE,
)
_TASK_ID_RE = re.compile(r"\b(TASK-\d+)\b", re.IGNORECASE)
_SUBJECT_PREFIX_RE = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


def _norm_subject_key(text: Any) -> str:
    cleaned = _SUBJECT_PREFIX_RE.sub("", str(text or "").strip())
    return " ".join(cleaned.lower().split())


def _is_mc_approval_spam(item: Any) -> bool:
    """True when a raw matter dict or ``AutopilotPrepared`` card is a Mission Control
    approval, merge-gate, or automated CI approval item that should stay out of the
    default human queue unless explicitly requested."""
    if isinstance(item, Mapping):
        if str(item.get("kind_of_matter") or "").strip().lower() == "approval":
            return True
        detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
        if str(detail.get("autopilot") or "").strip().lower() in _MC_APPROVAL_AUTOPILOTS:
            return True
        if str(item.get("autopilot") or "").strip().lower() in _MC_APPROVAL_AUTOPILOTS:
            return True
        if str(detail.get("approval") or "").startswith("MCApproval:"):
            return True
        fields = (
            item.get("subject"), item.get("summary"), item.get("note"),
            item.get("message"), item.get("sender"),
        )
    else:
        if str(getattr(item, "autopilot", "") or "").strip().lower() in _MC_APPROVAL_AUTOPILOTS:
            return True
        fields = (
            getattr(item, "subject", ""),
            getattr(item, "summary", ""),
            getattr(item, "note", ""),
            getattr(item, "message", ""),
            getattr(item, "sender", ""),
        )
    combined = " ".join(str(f or "") for f in fields if f)
    return bool(combined and _MC_APPROVAL_SPAM_RE.search(combined))


def _dedupe_prepared_items(items: Sequence[m.AutopilotPrepared]) -> list[m.AutopilotPrepared]:
    """Deduplicate identical titles/subjects or repeated ``TASK-NNNN`` items across
    matters and legacy queue cards, keeping the newest (first after descending sort).
    A legacy queue card (``ap_...``) with a distinct summary never hides a real matter."""
    out: list[m.AutopilotPrepared] = []
    seen_task_ids: set[str] = set()
    seen_titles: set[tuple[str, str, str]] = set()
    seen_exact: set[tuple[str, str, str]] = set()
    for item in items:
        is_legacy_card = str(item.item_id or "").startswith("ap_")
        family = "queue" if is_legacy_card else "matter"
        subj_norm = _norm_subject_key(item.subject)
        sum_norm = " ".join(str(item.summary or "").lower().split())
        sender_norm = str(item.sender or "").strip().lower()
        combined = f"{item.subject or ''} {item.summary or ''}"
        task_match = _TASK_ID_RE.search(combined)
        if task_match:
            tid = task_match.group(1).upper()
            if tid in seen_task_ids:
                continue
            seen_task_ids.add(tid)
        title_key = subj_norm or sum_norm
        if title_key:
            if sum_norm:
                exact_key = (title_key, sum_norm, sender_norm)
                if exact_key in seen_exact:
                    continue
                seen_exact.add(exact_key)
            # Collapse repeated identical titles within the same family (matters or queue cards)
            pair_key = (family, title_key, sender_norm or str(item.channel or ""))
            if pair_key in seen_titles:
                continue
            seen_titles.add(pair_key)
        out.append(item)
    return out


def _as_utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _has_open_matters(pg: Any, owners: set[str]) -> bool:
    """True when Postgres ``assist.matter_items`` holds at least one open or unverified
    matter for ``owners`` (best-effort; returns False on fake pools or absent tables)."""
    if not owners:
        return False
    try:
        exists = pg.execute("SELECT to_regclass('assist.matter_items') IS NOT NULL").fetchone()
        if not exists or exists[0] is not True:
            return False
        row = pg.execute(
            "SELECT EXISTS (SELECT 1 FROM assist.matter_items "
            "WHERE lower(owner_person) = ANY(%s) AND state IN ('open', 'unverified'))",
            (sorted(o.lower() for o in owners if o),),
        ).fetchone()
        return bool(row and row[0] is True)
    except Exception:  # noqa: BLE001
        return False


def _already_replied_matter_ids(
    pg: Any,
    reader_ids: set[str],
    reader_emails: set[str],
    items: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return ``item_id`` values of open conversation matters where ``assist.readings``,
    ``biz.emails``, or ``biz.teams_messages`` already records a newer reply sent by the
    reader on the same thread/subject after the matter's ``basis_at``."""
    conv = [
        i for i in items
        if str(i.get("kind_of_matter") or "").strip().lower() == "conversation"
        and _as_utc(i.get("basis_at")) is not None
    ]
    if not conv or (not reader_ids and not reader_emails):
        return set()
    answered: set[str] = set()
    mids = [str(i["item_id"]) for i in conv if i.get("item_id")]
    basis_by_id = {str(i["item_id"]): _as_utc(i.get("basis_at")) for i in conv if i.get("item_id")}
    # 1. Check assist.readings for a newer reading from this reader in the same thread_key
    try:
        has_tables = pg.execute(
            "SELECT to_regclass('assist.matters') IS NOT NULL "
            "AND to_regclass('assist.readings') IS NOT NULL"
        ).fetchone()
        if has_tables and has_tables[0] is True and reader_ids:
            rows = pg.execute(
                "SELECT m.matter_id FROM assist.matters m "
                "WHERE m.matter_id = ANY(%s) AND m.kind = 'conversation' "
                "AND EXISTS ("
                "  SELECT 1 FROM assist.readings r "
                "  WHERE coalesce(r.thread_key, r.message_key) = m.subject_key "
                "    AND lower(coalesce(r.sender_person, '')) = ANY(%s) "
                "    AND r.said_at > m.basis_at"
                ")",
                (mids, sorted(o.lower() for o in reader_ids if o)),
            ).fetchall()
            for r in rows or ():
                if r and isinstance(r[0], str):
                    answered.add(r[0])
    except Exception:  # noqa: BLE001
        pass

    # 2. Check biz.emails for a newer reply sent by this reader on the same normalized subject
    remaining_mail = [
        i for i in conv
        if str(i.get("item_id")) not in answered
        and str(i.get("channel") or "").lower() == "mail"
        and _norm_subject_key(i.get("subject"))
    ]
    if remaining_mail and reader_emails:
        try:
            has_emails = pg.execute("SELECT to_regclass('biz.emails') IS NOT NULL").fetchone()
            if has_emails and has_emails[0] is True:
                min_basis = min(basis_by_id[str(i["item_id"])] for i in remaining_mail)
                sent_rows = pg.execute(
                    "SELECT subject, coalesce(sent_at, received_at) "
                    "FROM biz.emails "
                    "WHERE lower(coalesce(sender, '')) = ANY(%s) "
                    "  AND coalesce(sent_at, received_at) > %s "
                    "  AND subject IS NOT NULL AND subject <> ''",
                    (sorted(reader_emails), min_basis),
                ).fetchall()
                latest_by_subj: dict[str, datetime] = {}
                for subj, at_val in sent_rows or ():
                    at_dt = _as_utc(at_val)
                    key = _norm_subject_key(subj)
                    if key and at_dt is not None:
                        prev = latest_by_subj.get(key)
                        if prev is None or at_dt > prev:
                            latest_by_subj[key] = at_dt
                for i in remaining_mail:
                    iid = str(i["item_id"])
                    key = _norm_subject_key(i.get("subject"))
                    sent_dt = latest_by_subj.get(key)
                    b_dt = basis_by_id.get(iid)
                    if sent_dt is not None and b_dt is not None and sent_dt > b_dt:
                        answered.add(iid)
        except Exception:  # noqa: BLE001
            pass

    # 3. Check biz.teams_messages for a newer Teams message from this reader in the same chat
    remaining_teams = [
        i for i in conv
        if str(i.get("item_id")) not in answered
        and str(i.get("channel") or "").lower() == "teams"
    ]
    if remaining_teams and (reader_ids or reader_emails):
        try:
            has_teams = pg.execute(
                "SELECT to_regclass('biz.teams_messages') IS NOT NULL "
                "AND to_regclass('assist.matters') IS NOT NULL"
            ).fetchone()
            if has_teams and has_teams[0] is True:
                t_mids = [str(i["item_id"]) for i in remaining_teams]
                sender_tokens = sorted({*(o.lower() for o in reader_ids if o), *reader_emails})
                rows = pg.execute(
                    "SELECT m.matter_id FROM assist.matters m "
                    "WHERE m.matter_id = ANY(%s) AND m.kind = 'conversation' "
                    "AND EXISTS ("
                    "  SELECT 1 FROM biz.teams_messages tm "
                    "  WHERE tm.chat = m.subject_key "
                    "    AND lower(coalesce(tm.sender, '')) = ANY(%s) "
                    "    AND tm.sent_at > m.basis_at"
                    ")",
                    (t_mids, sender_tokens),
                ).fetchall()
                for r in rows or ():
                    if r and isinstance(r[0], str):
                        answered.add(r[0])
        except Exception:  # noqa: BLE001
            pass

    return answered


def matters_list(backend: Any, reader: Reader, *, all_ages: bool = False,
                 fresh: bool = False, include_queue: bool = True,
                 matter_id: Optional[str] = None,
                 include_approvals: bool = False,
                 kind: Optional[str] = None) -> m.AutopilotDrafts:
    """The reader's matters as ``AutopilotDrafts`` (module text); ``fresh``: the queue's
    cards read now, not from the kept list (an answer to a write). Mission Control
    merge-gate / automated CI approvals stay out of the default human queue unless
    ``include_approvals=True`` or ``kind='approvals'``."""
    from operating import assist_matters as am
    from operating import autopilot_engine as ae
    approvals_only = str(kind or "").strip().lower() == "approvals"
    show_approvals = bool(include_approvals or approvals_only)
    pool = _pool(backend)
    read_queue = backend._prepared_read if fresh else backend.autopilot_prepared_list
    if pool is None:      # no matters here: the queue's own cards only, and it is said
        queue = read_queue(reader) if include_queue else m.AutopilotDrafts(items=[], never_senders=[])
        qitems = [
            qi for qi in queue.items
            if (show_approvals if _is_mc_approval_spam(qi) else not approvals_only)
        ]
        qitems = _dedupe_prepared_items(sorted(qitems, key=lambda i: i.created_at, reverse=True))
        return m.AutopilotDrafts(items=qitems, never_senders=queue.never_senders,
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
    extra = {"matter_id": matter_id} if matter_id is not None else {}

    def read_matters() -> dict[str, Any]:
        with pool.connection() as pg:
            effective_shown = shown
            # Operators with open assist.matters in Postgres see their own door-checked
            # matters even if they have not opted into autopilot writes (keep rows=None
            # strictly fail-closed as opt_in_unreadable).
            if not owner and effective_shown is False and _has_open_matters(pg, ids | pools):
                effective_shown = True
            return am.as_items(pg, ids | pools, shown=effective_shown, owner_view=owner,
                               everyone=owner, all_ages=all_ages, door=_door(backend, reader),
                               names_of=_names_of(backend), **extra)

    if include_queue and matter_id is None:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="assist-drafts") as ex:
            f_matters = ex.submit(read_matters)
            f_queue = ex.submit(read_queue, reader)
            got = f_matters.result()
            queue_items = list(f_queue.result().items)
    else:
        got = read_matters()
        queue_items = list(read_queue(reader).items) if include_queue else []
    never = backend._apa_never(state, ids)
    # a matter made from one of the remaining queue triggers' items is listed as that item's
    # own card (its prepared changes, holds and links), read as the queue lists it; automated
    # internal system monitoring alerts (staging/UAT health checks, BC sync, email-relay,
    # cos@petrasoap.com PR merge-class gates) are excluded from the employee's queue
    from operating.assist_readings import is_automated_system_alert
    from .home_needs import are_same_event_raw_matters, raw_matter_richness
    seen_sigs: set[tuple[str, str, str, str]] = set()
    kept_raw: list[Mapping[str, Any]] = []
    for i in got["items"]:
        if i["queue_card"]:
            continue
        is_mc_spam = i["kind_of_matter"] == am.MC_APPROVAL_KIND or _is_mc_approval_spam(i)
        if is_mc_spam and not show_approvals:
            continue
        if approvals_only and not is_mc_spam:
            continue
        if not is_mc_spam and is_automated_system_alert(
            sender=i.get("sender"),
            subject=i.get("subject"),
            summary=i.get("summary"),
            note=i.get("note"),
            message=i.get("message"),
        ):
            continue
        sig = (
            str(i.get("owner") or "").lower(),
            str(i.get("kind_of_matter") or ""),
            " ".join(str(i.get("summary") or "").lower().split()),
            " ".join(str(i.get("note") or "").lower().split()),
        )
        if matter_id is None and sig[2] and sig in seen_sigs:
            continue
        if sig[2]:
            seen_sigs.add(sig)
        if matter_id is None:
            dup_idx = next(
                (idx for idx, existing in enumerate(kept_raw) if are_same_event_raw_matters(existing, i)),
                None,
            )
            if dup_idx is not None:
                if raw_matter_richness(i) > raw_matter_richness(kept_raw[dup_idx]):
                    kept_raw[dup_idx] = i
                continue
        kept_raw.append(i)

    # Suppress open conversation matters where the reader already sent a newer reply on the
    # same thread or subject after basis_at.
    if matter_id is None and kept_raw:
        reader_emails = {
            str(v).strip().lower()
            for v in (
                getattr(reader, "email", None),
                *(getattr(getattr(reader, "session", None), "identities", ()) or ()),
            )
            if v and "@" in str(v)
        }
        try:
            with pool.connection() as pg:
                replied_ids = _already_replied_matter_ids(pg, ids, reader_emails, kept_raw)
        except Exception:  # noqa: BLE001
            replied_ids = set()
        if replied_ids:
            kept_raw = [i for i in kept_raw if str(i.get("item_id")) not in replied_ids]

    items = _put_off(backend, state, [_prepared(i) for i in kept_raw])
    if include_queue:
        filtered_queue = [
            qi for qi in queue_items
            if (show_approvals if _is_mc_approval_spam(qi) else not approvals_only)
        ]
        items = sorted(items + filtered_queue, key=lambda i: i.created_at, reverse=True)
    if matter_id is None:
        items = _dedupe_prepared_items(items)
    return m.AutopilotDrafts(items=items, never_senders=never, older=got["older"],
                             status=got["status"], reason=got["reason"],
                             stale_since=got["as_of"].isoformat()
                             if got["status"] == "stale" and got.get("as_of") else None)


def _prepared(i: Mapping[str, Any]) -> m.AutopilotPrepared:
    from operating.assist_matters import clean_note_spacing, clean_user_text
    def iso(v: Any) -> Optional[str]:
        return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)
    todos = [
        m.AutopilotTodo(
            system=str(t.get("system") or ""),
            record=clean_user_text(t.get("record") or ""),
            what=clean_user_text(t.get("what") or ""),
        )
        for t in (i.get("todos") or ())
        if isinstance(t, Mapping) and str(t.get("what") or "").strip()
    ]
    options = [
        m.AutopilotOption(
            label=clean_user_text(o.get("label") or ""),
            reply=clean_user_text(o.get("reply") or ""),
        )
        for o in (i.get("options") or ())
        if isinstance(o, Mapping) and str(o.get("reply") or "").strip()
    ]
    facts = [clean_user_text(f) for f in (i.get("facts") or ()) if str(f or "").strip()]
    return m.AutopilotPrepared(
        item_id=i["item_id"], sender=clean_user_text(i["sender"]), channel=i["channel"],
        subject=clean_user_text(i["subject"]), message=clean_user_text(i["message"]),
        kind=i["kind"], summary=clean_user_text(i["summary"]),
        note=clean_note_spacing(clean_user_text(i["note"])),
        body=clean_user_text(i["body"]), to=list(i["to"]), ask=clean_user_text(i.get("ask", "")),
        facts=facts, todos=todos, options=options, redo=bool(i.get("redo", False)),
        created_at=iso(i["basis_at"]) or "",
        state=i["state"], verified_at=iso(i["verified_at"]), plan_status=i["plan_status"],
        plan_reason=i["plan_reason"], plan_at=iso(i.get("plan_at")),
        requested_at=iso(i.get("requested_at")))


def _matter(backend: Any, reader: Reader, item_id: str) -> dict[str, Any]:
    """The reader's own listed matter (as the list shows it, through the door), else 404."""
    got = matters_list(backend, reader, all_ages=True, include_queue=False, matter_id=item_id)
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


def decide_matter(backend: Any, reader: Reader, item_id: str,
                  body: m.AutopilotDecisionRequest, *, notes: Any = None) -> m.AutopilotItem:
    """Done, not needed (reject) or snooze on an Assist matter of the reader's own.
    Nothing is sent."""
    found = _matter(backend, reader, item_id)
    if found["legacy"]:
        item = backend.autopilot_decide(reader, found["legacy"], body)
        if notes is not None:
            notes.autopilot_decided(reader, item)
        _bump_assist_drafts(backend)
        return item
    if body.decision not in ("accept", "reject", "snooze"):
        raise UserFacing("action_refused", "This item can be marked done, not needed or "
                         "put off until later.", 409)
    _journal_decision(backend, reader, item_id, body)
    if body.decision != "snooze":
        _log(backend, item_id, "closed", {"by": sorted(_ids(backend, reader))[:1],
                                          "decision": body.decision, "note": body.note or ""})
    _bump_assist_drafts(backend)
    return _decided(item_id, found["owner"], body.decision)


def reopen_matter(backend: Any, reader: Reader, item_id: str, *,
                  reason: Optional[str] = None) -> bool:
    """Undo a closed/snoozed decision on an Assist matter in the decisions journal so it
    returns to the reader's Assist and Home lists immediately."""
    from datetime import datetime, timezone
    from .autopilot_area import DECISIONS_FILE, _iso
    from operating.card_actions import Journal
    state = backend._apa_state()
    if state is None:
        return False
    me = set(backend._apa_me(reader))
    is_owner = bool(backend._apa_is_owner(reader))
    legacy_id: Optional[str] = None
    pool = _pool(backend)
    if pool is not None:
        try:
            with pool.connection() as pg:
                row = pg.execute(
                    "SELECT legacy_item_id, owner_person FROM assist.matter_items WHERE matter_id = %s",
                    (item_id,),
                ).fetchone()
            if row is not None:
                owner_person = str(row[1] or "").strip().lower()
                ids = _ids(backend, reader) | set(backend._apa_pools(reader))
                if not (is_owner or owner_person in ids):
                    return False
                legacy_id = row[0]
        except Exception:  # noqa: BLE001 - fall back to journal check below
            legacy_id = None
    had_decision = False
    decisions_path = state / DECISIONS_FILE
    if decisions_path.exists():
        for entry in Journal(decisions_path).rows():
            eid = str(entry.get("item_id") or "")
            if eid in (item_id, legacy_id) and (is_owner or entry.get("by") in me):
                had_decision = True
    if not had_decision and legacy_id is None:
        return False
    now = datetime.now(timezone.utc)
    by = sorted(me)[0]
    note = (reason or "").strip() or None
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Journal(decisions_path).locked() as held:
        held.append({"event": "item_decision", "item_id": item_id, "decision": "open",
                     "at": _iso(now), "by": by, "note": note, "matter": True})
        if legacy_id:
            held.append({"event": "item_decision", "item_id": legacy_id, "decision": "open",
                         "at": _iso(now), "by": by, "note": note})
    _bump_assist_drafts(backend)
    return True


def install(app: Any, backend: Any, reader: Callable[..., Reader], notes: Any) -> None:
    """Register the routes (before the queue's item routes: see the module text)."""

    @app.get("/api/autopilot/drafts", response_model=m.AutopilotDrafts, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def assist_list(all_ages: bool = False, include_approvals: bool = False,
                    kind: Optional[str] = None,
                    who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        """Your open work, newest first: each item kept true against its source; the last
        14 days unless ``all_ages`` (``older`` says how many more). Pass
        ``include_approvals=true`` or ``kind=approvals`` to inspect Mission Control
        approvals."""
        if not include_approvals and not kind:
            cached = getattr(backend, "assist_drafts", None)
            if callable(cached):
                return cached(who, all_ages=all_ages)
        return matters_list(backend, who, all_ages=all_ages,
                            include_approvals=include_approvals, kind=kind)

    @app.post("/api/autopilot/items/{item_id:matter}/draft", response_model=m.AssistWorking,
              tags=["actions"], responses=_ERR)
    def assist_draft(item_id: str, who: Reader = Depends(reader)) -> m.AssistWorking:
        """Draft this item now (it goes to the front of the drafting queue)."""
        _matter(backend, who, item_id)
        _log(backend, item_id, "requested", {"by": sorted(_ids(backend, who))[:1]})
        _bump_assist_drafts(backend)
        return m.AssistWorking(status="working")

    @app.post("/api/autopilot/items/{item_id:matter}/decision", response_model=m.AutopilotItem,
              tags=["actions"], responses=_ERR)
    def assist_decide(item_id: str, body: m.AutopilotDecisionRequest,
                      who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Done, not needed (reject) or snooze on an item of yours. Nothing is sent."""
        return decide_matter(backend, who, item_id, body, notes=notes)

    @app.post("/api/autopilot/items/{item_id:matter}/reply-from-line",
              response_model=m.AutopilotItem, tags=["actions"], responses=_ERR)
    def assist_line(item_id: str, body: m.AutopilotLineRequest,
                    who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Your one line for this item: the draft is written from it."""
        found = _matter(backend, who, item_id)
        if found["legacy"]:
            res = backend.autopilot_reply_from_line(who, found["legacy"], body)
            _bump_assist_drafts(backend)
            return res
        _log(backend, item_id, "person_line", {"by": sorted(_ids(backend, who))[:1],
                                               "line": body.line})
        _log(backend, item_id, "requested", {"by": sorted(_ids(backend, who))[:1]})
        _bump_assist_drafts(backend)
        return _decided(item_id, found["owner"], None)

    @app.post("/api/autopilot/items/{item_id:matter}/hand", response_model=m.AutopilotDrafts,
              tags=["actions"], responses=_ERR)
    def assist_hand(item_id: str, body: m.AutopilotHandRequest,
                    who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        found = _matter(backend, who, item_id)
        if not found["legacy"]:
            raise UserFacing("action_refused", "This item cannot be passed on yet.", 409)
        backend.autopilot_hand(who, found["legacy"], body)
        _bump_assist_drafts(backend)
        return matters_list(backend, who, fresh=True)

    @app.post("/api/autopilot/items/{item_id:matter}/take", response_model=m.AutopilotDrafts,
              tags=["actions"], responses=_ERR)
    def assist_take(item_id: str, who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        found = _matter(backend, who, item_id)
        if not found["legacy"]:
            raise UserFacing("action_refused", "This item cannot be taken yet.", 409)
        backend.autopilot_take(who, found["legacy"])
        _bump_assist_drafts(backend)
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
    from operating.card_actions import Journal
    from .autopilot_area import DECISIONS_FILE, _instant
    if state is None or not (state / DECISIONS_FILE).exists():
        return list(items)
    said: dict[str, dict[str, Any]] = {}
    for row in Journal(state / DECISIONS_FILE).rows():
        if row.get("matter"):
            iid = str(row.get("item_id"))
            if row.get("decision") == "open":
                said.pop(iid, None)
            else:
                said[iid] = row
    if not said:
        return list(items)
    now = datetime.now(timezone.utc)
    out = []
    for i in items:
        d = said.get(i.item_id)
        at = _instant(d.get("at")) if d else None
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
