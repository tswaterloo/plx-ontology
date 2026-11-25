"""``plx.sql {"record": "<record handle or number>"}``: where is X, in one read (ASK-GENIE 20 #3,
19 S1 & S9): the record's card across systems with stored thread state.

The card is the chain read of ``ontology_tools_chain`` at one hop (hop 0 is the record
itself with the facts its type's catalog declares: status, dates, amounts; hop 1 every
record linked to it, grouped by type with its label, date, system and facts) plus the
messages that name it: every mail and Teams message whose ``records`` hold the record's
handle, newest first, as a result of its own (its own id, paged by ``{"page": id, "offset": n}``,
none cut), the newest few shown in the card alongside their stored ``assist.readings`` thread
state ("did they reply, who owes whom", waiting_on, due, summary — ASK-GENIE 19 S9). Nothing here
decides what "the status" is: every declared field is shown, and which of two records a number
means is the chain read's rule (a number that fits unlinked records comes back as candidates to
pick from, never one taken).

Permissions are the door's: both reads go through ``plx.sql``'s own statement check, scope and
views, so a record, link or message the reader may not read is not in the card. Stored readings
from ``assist.readings`` (through ``assist.card_readings`` when installed) are joined ONLY on
the message handles already returned by the reader's guarded ``messages_sql`` query, and every
reading's identities and text fields are checked against the people exclusion and sensitive
content rules (fail closed) and masked for card numbers and secrets before leaving."""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Mapping, Sequence

from operating import card_text
from operating import ontology_tools_chain as chain
from operating.ontology_tools_identity import ToolError
from operating.pg.sql_views import _literal

log = logging.getLogger(__name__)

SHOWN = 10   # messages shown inside the card; the result of the messages holds and pages them all
CARD_READINGS_VIEW = "assist.card_readings"
CARD_READINGS_TABLE = "assist.readings"
READINGS_SOURCE_NAME = "assist.readings"

_SOURCE_TO_READINGS = {"mail": "emails", "teams": "teams_messages"}
_READINGS_COLS = (
    "source", "message_key", "handles", "thread_key", "said_at", "from_side",
    "needs_act", "actor", "actor_person", "actor_basis", "sender_person", "act", "act_class",
    "due", "promise", "waiting_on", "counterpart", "summary", "read_ver", "read_at",
)
_GUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _starts(tool: Any, result: str) -> list[str]:
    """The handles of the start records of a chain result (hop 0, direction start)."""
    from operating.ontology_tools_sql_rows import RESULTS
    kept = RESULTS.get(result.split("//", 1)[-1])
    if kept is None:
        return []
    cols = list(kept["columns"])
    hop, way, handle = cols.index("hop"), cols.index("direction"), cols.index("handle")
    return [r[handle] for r in kept["rows"]
            if r[hop] == 0 and r[way] == "start" and isinstance(r[handle], str)]


SOURCES = (
    ("emails", "SELECT 'mail' AS source, handle, received_at AS at, subject AS title, "
               "sender AS who FROM emails WHERE records && {have}"),
    ("teams_messages", "SELECT 'teams', handle, created_at, subject, author "
                       "FROM teams_messages WHERE records && {have}"))


def messages_sql(handles: list[str], views: tuple[str, ...] = ("emails", "teams_messages")
                 ) -> str:
    """One statement over the messages (mail and Teams) whose ``records`` hold any of
    ``handles``, newest first."""
    have = "ARRAY[" + ", ".join(_literal(h) for h in handles) + "]::text[]"
    parts = [sql.format(have=have) for view, sql in SOURCES if view in views]
    return " UNION ALL ".join(parts) + " ORDER BY at DESC, handle"


def _raw_cell(value: Any) -> Any:
    """Unwrap an ``untrusted_text`` envelope cell to its plain string for internal matching."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"]
    return value


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _is_usable_guard(guard: Any) -> bool:
    """Whether ``guard`` exposes the exclusion-guard interface (duck-typed)."""
    return (
        guard is not None
        and all(hasattr(guard, attr) for attr in ("is_excluded", "decides", "unavailable"))
    )


def _unwrap_guard(guard: Any) -> Any:
    """Resolve a ``ReloadingExclusion`` (via ``.current()``) or accept a duck-typed guard."""
    if guard is None:
        return None
    current = getattr(guard, "current", None)
    if callable(current):
        try:
            guard = current()
        except Exception as exc:  # noqa: BLE001 - unreadable exclusion fails closed
            log.warning("card readings exclusion current() failed (%s)", type(exc).__name__)
            return None
    return guard if _is_usable_guard(guard) else None


def _people_guard(tool: Any) -> Any:
    """The active people-exclusion guard for the host (bound ``address_exclusion`` on
    runtime when available, else ``people_exclusion`` on runtime / item_gate / base,
    supporting ``ReloadingExclusion`` and duck-typed guards, else loaded from config)."""
    from integrations.people_exclusion import PeopleExclusion
    base = getattr(tool, "base", None)
    item_gate = getattr(base, "item_gate", None)
    holders = (
        getattr(base, "runtime", None),
        getattr(item_gate, "runtime", None),
        item_gate,
        base,
    )
    for holder in holders:
        if holder is None:
            continue
        addr_fn = getattr(holder, "address_exclusion", None)
        if callable(addr_fn):
            try:
                addr_guard = _unwrap_guard(addr_fn())
            except Exception as exc:  # noqa: BLE001 - address binding failure falls back or fails closed
                log.warning("card readings address_exclusion() failed (%s)", type(exc).__name__)
                addr_guard = None
            if addr_guard is not None and not addr_guard.unavailable:
                return addr_guard
    for holder in holders:
        if holder is None:
            continue
        raw_guard = getattr(holder, "people_exclusion", None)
        if raw_guard is None:
            continue
        guard = _unwrap_guard(raw_guard)
        if guard is not None:
            return guard
        return PeopleExclusion(unavailable="excluded_people_file_unreadable")
    from operating.pg import permissions
    return permissions.load_exclusion()


def _is_owner(tool: Any, actor: Any) -> bool:
    from operating.pg import people_excluded
    base = getattr(tool, "base", None)
    return bool(base is not None and people_excluded.owner_reader(base, actor))


def _reading_excluded(row: Mapping[str, Any], guard: Any) -> bool:
    """Whether ``row`` names an excluded person in its structured identity fields or text.
    Fail closed on an unavailable guard or undecidable exclusion check."""
    from integrations.people_exclusion import ExclusionUnavailable, is_object_id
    guard = _unwrap_guard(guard)
    if guard is None or guard.unavailable:
        return True
    text_fields = [str(row.get(k) or "") for k in (
        "actor", "act", "promise", "waiting_on", "counterpart", "summary")]
    combined_low = " ".join(text_fields).lower()
    # Check bound addresses / excluded OIDs directly in text fields
    for addr in getattr(guard, "_addresses", ()) or ():
        if addr and addr in combined_low:
            return True
    for oid in getattr(guard, "_ids", ()) or ():
        if oid and oid in combined_low:
            return True
    candidates: list[str] = []
    for k in ("actor_person", "sender_person", "actor"):
        val = row.get(k)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip().lower())
    for text in text_fields:
        if text:
            candidates.extend(m.lower() for m in _GUID_RE.findall(text))
            candidates.extend(m.lower() for m in _EMAIL_RE.findall(text))
    for ident in dict.fromkeys(candidates):
        try:
            if is_object_id(ident):
                if guard.is_excluded(ident):
                    return True
            elif "@" in ident:
                if not guard.decides(ident) or guard.is_excluded(ident):
                    return True
        except ExclusionUnavailable:
            return True
    return False


def _reading_sensitive(row: Mapping[str, Any], scope: Any, msg_title: str) -> bool:
    """Whether ``row`` carries personal-financial / HR / sensitive-labelled text that the
    reader lacks markings to read (fail closed on detector errors)."""
    from operating.chunking import sensitive
    held = set(getattr(scope, "held", ()) or ())
    if set(sensitive.MARKS).issubset(held):
        return False
    parts = [str(row.get(k) or "") for k in (
        "summary", "act", "promise", "waiting_on", "counterpart", "actor")]
    combined = "\n".join(p for p in parts if p.strip())
    if not combined:
        return False
    try:
        if sensitive.personal_financial(combined, title=msg_title or None) is not None:
            return True
        if sensitive.labelled([combined, msg_title or None]):
            return True
    except Exception as exc:  # noqa: BLE001 - undecidable sensitive screen: fail closed
        log.warning("card readings sensitive screen failed, withholding row: %s",
                    type(exc).__name__)
        return True
    return False


def _mask_text(value: Any) -> str:
    text = str(value or "").strip()
    return card_text.mask_served(text) if text else ""


def _who_owes_whom(row: Mapping[str, Any]) -> str | None:
    """Structured who-owes-whom direction derived solely from ``assist.readings`` columns
    (``needs_act``, ``actor_person``, ``actor``, ``from_side``, ``waiting_on``, ``promise``)."""
    needs_act = row.get("needs_act")
    from_side = str(row.get("from_side") or "").strip()
    actor = str(row.get("actor") or "").strip()
    actor_person = str(row.get("actor_person") or "").strip()
    actor_basis = str(row.get("actor_basis") or "").strip()
    waiting_on = str(row.get("waiting_on") or "").strip()
    promise = str(row.get("promise") or "").strip()
    if needs_act is True:
        if actor_person:
            if from_side in ("customer", "outside"):
                return "we_owe_them"
            if from_side == "us":
                return "internal"
            return "we_owe"
        if actor == "outside" or (actor and from_side == "us"
                                  and not actor_basis.startswith("unresolved:")):
            return "they_owe_us"
        return "action_needed"
    if needs_act is False:
        if waiting_on:
            return "they_owe_us" if from_side == "us" else "waiting"
        if promise:
            return "we_owe_them" if from_side == "us" else (
                "they_owe_us" if from_side in ("customer", "outside") else "promised")
        return "settled"
    return None


def _fetch_readings(conn: Any, visible_handles: Sequence[str]
                    ) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Read stored ``ok`` readings for ``visible_handles`` ONLY in one read-only statement.

    Reads ``assist.card_readings`` first (the API role's narrow view from migration 0085),
    falling back to ``assist.readings`` (filtered ``status = 'ok'``) where the view is not
    installed yet. Returns ``(rows, None)`` on success or ``(None, reason)`` when absent or
    unreadable (never raises to break the card)."""
    if not visible_handles:
        return [], None
    import psycopg
    from operating import ontology_tools_sql as sql_mod

    cols_sql = ", ".join(_READINGS_COLS)
    try:
        with sql_mod._lent(conn) as pg:
            kept = (pg.isolation_level, pg.read_only)
            try:
                pg.isolation_level, pg.read_only = psycopg.IsolationLevel.REPEATABLE_READ, True
                with pg.transaction():
                    rel = None
                    where_extra = ""
                    if pg.execute("SELECT to_regclass(%s)", (CARD_READINGS_VIEW,)).fetchone()[0]:
                        rel = CARD_READINGS_VIEW
                    elif pg.execute("SELECT to_regclass(%s)", (CARD_READINGS_TABLE,)).fetchone()[0]:
                        rel = CARD_READINGS_TABLE
                        where_extra = "status = 'ok' AND "
                    else:
                        return None, "not_built: assist.readings is not built in this store"
                    sql = (f"SELECT {cols_sql} FROM {rel} "
                           f"WHERE {where_extra}handles && %s::text[] "
                           "ORDER BY read_ver DESC, read_at DESC")
                    raw = pg.execute(sql, (list(visible_handles),)).fetchall()
            finally:
                if not pg.closed and pg.info.transaction_status == 0:
                    pg.isolation_level, pg.read_only = kept
    except psycopg.errors.InsufficientPrivilege:
        log.warning("card readings unavailable: insufficient privilege on assist readings")
        return None, "not_readable: no permission to read assist.readings"
    except psycopg.errors.UndefinedTable:
        log.warning("card readings unavailable: assist.readings relation missing")
        return None, "not_built: assist.readings is not built in this store"
    except psycopg.Error as exc:
        msg = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        log.warning("card readings query failed (%s): %s", type(exc).__name__, msg)
        return None, f"query_failed: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 - soft degradation, logged and surfaced in card
        log.warning("card readings unexpected error (%s)", type(exc).__name__)
        return None, f"unavailable: {type(exc).__name__}"
    return [dict(zip(_READINGS_COLS, r)) for r in raw], None


def _different_sender(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether visible message ``b`` was sent by a different sender than ``a``."""
    sp_a, sp_b = a.get("sender_person"), b.get("sender_person")
    if sp_a and sp_b:
        return sp_a != sp_b
    who_a = str(a.get("who") or "").strip().lower()
    who_b = str(b.get("who") or "").strip().lower()
    if who_a and who_b:
        return who_a != who_b
    fs_a, fs_b = str(a.get("from_side") or ""), str(b.get("from_side") or "")
    if fs_a and fs_b:
        return fs_a != fs_b
    return False


def _enrich_with_readings(tool: Any, conn: Any, scope: Any, actor: Any,
                          columns: Sequence[str], newest: Sequence[Mapping[str, Any]]
                          ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    """Join stored ``assist.readings`` onto ``newest`` visible messages ONLY.

    Returns ``(enriched_newest, thread_state, unavailable_reason)``. Never mutates the
    original rows held in the result store."""
    copied = [dict(row) for row in newest]
    if not copied:
        return copied, None, None
    col_idx = {name: i for i, name in enumerate(columns)}
    i_src = col_idx.get("source")
    i_handle = col_idx.get("handle")
    i_at = col_idx.get("at")
    i_title = col_idx.get("title")
    i_who = col_idx.get("who")
    if i_src is None or i_handle is None:
        return copied, None, "invalid_columns: message result missing source or handle"

    # Extract only the visible messages returned by the reader's guarded query
    msg_meta: list[dict[str, Any]] = []
    visible_handles: list[str] = []
    for idx, row in enumerate(copied):
        cells = row.get("cells") or []
        src_label = str(_raw_cell(cells[i_src]) if i_src < len(cells) else "" or "").strip()
        handle = _raw_cell(cells[i_handle]) if i_handle < len(cells) else None
        at_val = _iso(_raw_cell(cells[i_at]) if i_at is not None and i_at < len(cells) else None)
        title = str(_raw_cell(cells[i_title]) if i_title is not None and i_title < len(cells)
                    else "" or "")
        who = str(_raw_cell(cells[i_who]) if i_who is not None and i_who < len(cells) else "" or "")
        reading_src = _SOURCE_TO_READINGS.get(src_label, "")
        handle_s = handle if isinstance(handle, str) and handle else ""
        msg_meta.append({"idx": idx, "source_label": src_label, "reading_source": reading_src,
                         "handle": handle_s, "at": at_val, "title": title, "who": who})
        if handle_s and reading_src:
            visible_handles.append(handle_s)

    visible_handles = list(dict.fromkeys(visible_handles))
    if not visible_handles:
        return copied, None, None

    # People exclusion guard check (fail closed for non-owner readers)
    owner = _is_owner(tool, actor)
    guard = None if owner else _people_guard(tool)
    if not owner and (guard is None or getattr(guard, "unavailable", "unknown")):
        why = getattr(guard, "unavailable", "excluded_people_unavailable")
        log.warning("card readings withheld: people exclusion unavailable (%s)", why)
        return copied, None, f"people_exclusion_unavailable: {why}"

    raw_rows, err = _fetch_readings(conn, visible_handles)
    if err is not None or raw_rows is None:
        return copied, None, err or "unavailable"

    # Map ONLY visible (reading_source, handle) pairs; prefer highest (read_ver, read_at)
    visible_pairs = {(m["reading_source"], m["handle"]) for m in msg_meta
                     if m["reading_source"] and m["handle"]}
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for r in raw_rows:
        r_src = str(r.get("source") or "")
        for h in r.get("handles") or ():
            pair = (r_src, h)
            if pair in visible_pairs and pair not in by_pair:
                by_pair[pair] = r

    # Filter each matched reading through people-exclusion & sensitive-content guards
    safe_by_idx: dict[int, dict[str, Any]] = {}
    withheld = 0
    for m in msg_meta:
        r = by_pair.get((m["reading_source"], m["handle"]))
        if r is None:
            continue
        if not owner and _reading_excluded(r, guard):
            withheld += 1
            continue
        if _reading_sensitive(r, scope, m["title"]):
            withheld += 1
            continue
        safe_by_idx[m["idx"]] = r

    # Compute visible thread reply state (deduplicating mailbox copies by message_key)
    thread_groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for m in msg_meta:
        r = safe_by_idx.get(m["idx"])
        if r is None:
            continue
        tkey = (m["reading_source"], str(r.get("thread_key") or r.get("message_key") or m["handle"]))
        mkey = str(r.get("message_key") or m["handle"])
        at_key = _iso(r.get("said_at")) or m["at"] or ""
        entry = {
            "idx": m["idx"],
            "source_label": m["source_label"],
            "handle": m["handle"],
            "message_key": mkey,
            "thread_key": str(r.get("thread_key") or "") or None,
            "at": m["at"] or _iso(r.get("said_at")),
            "sort_at": at_key,
            "who": m["who"],
            "sender_person": r.get("sender_person"),
            "from_side": str(r.get("from_side") or ""),
            "reading": r,
        }
        group = thread_groups.setdefault(tkey, {})
        prev = group.get(mkey)
        if prev is None or entry["sort_at"] > prev["sort_at"]:
            group[mkey] = entry

    replied_info: dict[tuple[str, str], tuple[bool, str | None]] = {}
    thread_summaries: list[dict[str, Any]] = []
    for tkey, unique_map in thread_groups.items():
        ordered = sorted(unique_map.values(), key=lambda x: (x["sort_at"], x["handle"]))
        for i, cur in enumerate(ordered):
            later_reply = next(
                (nxt for nxt in ordered[i + 1:] if _different_sender(cur, nxt)),
                None,
            )
            replied_info[(tkey[0], cur["message_key"])] = (
                later_reply is not None,
                later_reply["at"] if later_reply is not None else None,
            )
        latest = ordered[-1]
        r = latest["reading"]
        has_prior_other = any(_different_sender(earlier, latest) for earlier in ordered[:-1])
        needs_act = r.get("needs_act")
        waiting_on = _mask_text(r.get("waiting_on"))
        promise = _mask_text(r.get("promise"))
        who_owes = _who_owes_whom(r)
        if needs_act is True or waiting_on:
            thread_replied: bool | None = False
        elif has_prior_other and needs_act is False:
            thread_replied = True
        else:
            thread_replied = None
        summary_entry: dict[str, Any] = {
            "source": latest["source_label"],
            "latest_handle": latest["handle"],
            "latest_at": latest["at"],
            "visible_messages": len(ordered),
        }
        if latest["thread_key"]:
            summary_entry["thread_key"] = latest["thread_key"]
        if latest["from_side"]:
            summary_entry["from_side"] = latest["from_side"]
        if who_owes is not None:
            summary_entry["who_owes_whom"] = who_owes
        if needs_act is not None:
            summary_entry["needs_act"] = bool(needs_act)
            summary_entry["needs_reply"] = bool(needs_act and r.get("act_class") == "reply")
        if thread_replied is not None:
            summary_entry["replied"] = thread_replied
        for key, raw_val in (("actor", _mask_text(r.get("actor"))),
                             ("act", _mask_text(r.get("act"))),
                             ("act_class", r.get("act_class")),
                             ("due", _iso(r.get("due"))),
                             ("waiting_on", waiting_on),
                             ("promise", promise),
                             ("counterpart", _mask_text(r.get("counterpart"))),
                             ("summary", _mask_text(r.get("summary")))):
            if raw_val:
                summary_entry[key] = raw_val
        read_at_iso = _iso(r.get("read_at"))
        if read_at_iso:
            summary_entry["as_of"] = read_at_iso
        thread_summaries.append(summary_entry)

    thread_summaries.sort(key=lambda t: (t.get("latest_at") or "", t.get("latest_handle") or ""),
                          reverse=True)

    # Attach concise reading dict onto each enriched message row in card.newest
    latest_read_at: str | None = None
    for m in msg_meta:
        r = safe_by_idx.get(m["idx"])
        if r is None:
            continue
        mkey = str(r.get("message_key") or m["handle"])
        msg_replied, replied_at = replied_info.get((m["reading_source"], mkey), (False, None))
        needs_act = r.get("needs_act")
        who_owes = _who_owes_whom(r)
        read_at_iso = _iso(r.get("read_at"))
        if read_at_iso and (latest_read_at is None or read_at_iso > latest_read_at):
            latest_read_at = read_at_iso
        rec: dict[str, Any] = {}
        if r.get("thread_key"):
            rec["thread_key"] = str(r["thread_key"])
        if r.get("from_side"):
            rec["from_side"] = str(r["from_side"])
        if who_owes is not None:
            rec["who_owes_whom"] = who_owes
        if needs_act is not None:
            rec["needs_act"] = bool(needs_act)
            rec["needs_reply"] = bool(needs_act and r.get("act_class") == "reply")
        rec["replied"] = msg_replied
        if replied_at:
            rec["replied_at"] = replied_at
        for key, raw_val in (("actor", _mask_text(r.get("actor"))),
                             ("act", _mask_text(r.get("act"))),
                             ("act_class", r.get("act_class")),
                             ("due", _iso(r.get("due"))),
                             ("waiting_on", _mask_text(r.get("waiting_on"))),
                             ("promise", _mask_text(r.get("promise"))),
                             ("counterpart", _mask_text(r.get("counterpart"))),
                             ("summary", _mask_text(r.get("summary")))):
            if raw_val:
                rec[key] = raw_val
        if read_at_iso:
            rec["as_of"] = read_at_iso
        copied[m["idx"]]["reading"] = rec

    state: dict[str, Any] = {
        "source": READINGS_SOURCE_NAME,
        "messages_checked": len(copied),
        "readings_found": len(safe_by_idx),
        "threads": thread_summaries,
    }
    if latest_read_at:
        state["as_of"] = latest_read_at
    if withheld:
        state["withheld"] = withheld
    return copied, state, None


def _merge_views(out: dict[str, Any], extra_views: Sequence[str]) -> None:
    """Append ``extra_views`` onto ``out['views']`` (and the kept result entry) without
    duplicates, preserving order."""
    views = list(out.get("views") or [])
    for v in extra_views:
        if isinstance(v, str) and v and v not in views:
            views.append(v)
    out["views"] = views
    rid = str(out.get("result") or "").split("//", 1)[-1]
    if rid:
        from operating.ontology_tools_sql_rows import RESULTS
        kept = RESULTS.get(rid)
        if kept is not None:
            kept["views"] = list(views)


def card(tool: Any, conn: Any, scope: Any, actor: Any, args: Mapping[str, Any]
         ) -> dict[str, Any]:
    """The ``record`` read ``args`` asks as ``actor`` (see the module doc)."""
    asked = {"chain": args["record"], "hops": 1,
             **{k: args[k] for k in ("party", "type") if k in args}}
    out = chain.walk(tool, conn, scope, actor, asked)
    handles = [] if out.get("chain", {}).get("ambiguous") else _starts(tool, out["result"])
    mail: dict[str, Any] = {"records_named": len(handles)}
    out["note"] = (out.get("note", "") + " The card: hop 0 is the record, hop 1 its linked "
                   "records, card.newest the latest mail and Teams that name it (the rest "
                   "by page), and card.thread_state their stored reply / who-owes-whom "
                   "readings when available.").strip()
    if handles:
        try:
            got = tool._call({"sql": messages_sql(handles)}, actor)
        except ToolError as exc:
            # said in the card, never an empty list that reads as "no messages"
            mail["messages_unavailable"] = f"{exc.code}: {exc}"
            out["card"] = mail
            return out
        shown = got.get("rows") or []
        cols = got.get("columns") or []
        # guard-ok: SHOWN messages inside the card; the rest are paged via card.read_on
        newest_rows = shown[:SHOWN]
        enriched, thread_state, readings_err = _enrich_with_readings(
            tool, conn, scope, actor, cols, newest_rows)
        extra_views = list(got.get("views") or ())
        _merge_views(out, extra_views)
        mail.update({"result": got["result"], "messages": got.get("profile", {}).get("rows"),
                     "columns": cols, "newest": enriched,
                     "read_on": ('{"page": "%s", "offset": %d}' % (got["result"], SHOWN))
                     if len(shown) > SHOWN or got.get("continue") else None})
        if thread_state is not None:
            mail["readings_source"] = READINGS_SOURCE_NAME
            mail["thread_state"] = thread_state
        elif readings_err is not None:
            mail["readings_unavailable"] = readings_err
    else:
        mail["note"] = ("no single record was walked, so no messages were looked up: pick "
                        "one by handle, type or party")
    out["card"] = mail
    return out
